#!/usr/bin/env bash
# Idempotent first-time setup: workspace, data/ tree, secrets, n8n credentials.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"
# shellcheck source=lib/secret.sh
source "${ROOT}/scripts/lib/secret.sh"
# shellcheck source=lib/python.sh
source "${ROOT}/scripts/lib/python.sh"
# shellcheck source=lib/local-files.sh
source "${ROOT}/scripts/lib/local-files.sh"

WORKSPACE=""
WEBUI_TITLE=""
OPENCODE_WORKSPACE_HOST=""
ASSISTANT_DATA_ROOT_ARG=""
SETUP_OLLAMA=0
SETUP_RAG=0
SETUP_NEO4J=0
SETUP_AUTOMATION=0
SETUP_RESEARCH=0
SETUP_CODING=0
SETUP_CALENDAR=0
SETUP_DB=0
SETUP_TODO=0
SETUP_CRM=0
SETUP_BUDGET=0
SETUP_MAIL=0
SETUP_MAIL_DRAFTS=0
SETUP_MESSAGES=0
NON_INTERACTIVE=0

usage() {
  cat <<'EOF'
Usage: ./scripts/setup.sh [options]

Options:
  --workspace SLUG          Knowledge-base slug (alphanumeric + hyphens)
  --title TITLE             LightRAG web UI title
  --opencode-workspace PATH Host path for OpenCode workspace mount
  --data-root PATH          User content root (projects, voice, memory, inputs, …)
                            Hermes state stays at ./data/hermes. See docs/data-dir.md
  --rag                     Enable knowledge graph (compose profile rag; starts Neo4j)
  --neo4j                   Enable Neo4j alone, without LightRAG (profile neo4j)
  --automation              Enable n8n + GPT Researcher (profile automation)
  --research                Enable GPT Researcher deep research as Hermes tools, without n8n (profile research)
  --coding                  Enable OpenCode (profile coding)
  --calendar                Enable CalDAV calendar MCP (profile calendar)
  --db                      Enable shared Postgres + SQL MCP for Hermes (profile db)
  --todo                    Enable Vikunja todo lists + MCP (profile todo; starts the shared Postgres)
  --crm                     Enable a local Twenty CRM + its MCP for Hermes (profile crm; about 1.8 GiB RAM)
  --budget                  Enable a local Actual Budget server + its MCP for Hermes (profiles budget-local, budget)
  --mail                    Enable the read-only mail MCP over a host-built Apple Mail index (profile mail)
  --mail-drafts             Let Hermes queue email drafts that a host helper saves to Mail's Drafts; never sends (profile mail-drafts)
  --messages                Enable the read-only Messages (iMessage/SMS) MCP over a host-built index, with new-message alerts (profile messages)
  --ollama                  Bundle Ollama in Docker + demo model pulls (profile ollama)
  --non-interactive         Fail if required values are missing
  -h, --help                Show this help

Default COMPOSE_PROFILES=core (Hermes + WebUI + SearXNG).
Examples:
  ./scripts/setup.sh
  ./scripts/setup.sh --rag --automation --coding
  ./scripts/setup.sh --calendar
  ./scripts/setup.sh --db
  ./scripts/setup.sh --todo
  ./scripts/setup.sh --mail
  ./scripts/setup.sh --data-root ~/AssistantData
  ./scripts/setup.sh --ollama --non-interactive --workspace my-kb --title "My KB"
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --title) WEBUI_TITLE="$2"; shift 2 ;;
    --opencode-workspace) OPENCODE_WORKSPACE_HOST="$2"; shift 2 ;;
    --data-root) ASSISTANT_DATA_ROOT_ARG="$2"; shift 2 ;;
    --rag) SETUP_RAG=1; shift ;;
    --neo4j) SETUP_NEO4J=1; shift ;;
    --automation) SETUP_AUTOMATION=1; shift ;;
    --research) SETUP_RESEARCH=1; shift ;;
    --coding) SETUP_CODING=1; shift ;;
    --calendar) SETUP_CALENDAR=1; shift ;;
    --db) SETUP_DB=1; shift ;;
    --todo) SETUP_TODO=1; shift ;;
    --crm) SETUP_CRM=1; shift ;;
    --budget) SETUP_BUDGET=1; shift ;;
    --mail) SETUP_MAIL=1; shift ;;
    --mail-drafts) SETUP_MAIL_DRAFTS=1; shift ;;
    --messages) SETUP_MESSAGES=1; shift ;;
    --ollama) SETUP_OLLAMA=1; shift ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

log() { printf '[setup] %s\n' "$1"; }
die() { printf '[setup] ERROR: %s\n' "$1" >&2; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

rand_hex() {
  random_value hex "$1"
}

# 32 letters and digits, for values that go unquoted into URLs and configs.
rand_secret() {
  random_value base64 32 | tr -dc 'A-Za-z0-9' | head -c 32
}

validate_workspace() {
  local slug="$1"
  [[ "$slug" =~ ^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$ ]] || die "Invalid workspace slug: $slug (use letters, numbers, hyphens)"
}

# OpenCode mounts this path at the identical path inside the container, which
# means it has to be a usable absolute path on both sides.
validate_opencode_workspace() {
  local path="$1"
  [[ "$path" == /* ]] || die "OPENCODE_WORKSPACE_HOST must be an absolute path: $path"
  [[ "$path" != "/" ]] || die "OPENCODE_WORKSPACE_HOST must not be /; OpenCode gets read/write access to the whole mount"
  [[ "$path" != */ ]] || die "OPENCODE_WORKSPACE_HOST must not end with a slash: $path"
}

prompt_default() {
  local prompt="$1"
  local default="$2"
  local reply
  if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    echo "$default"
    return
  fi
  read -r -p "$prompt [$default]: " reply
  if [[ -z "$reply" ]]; then
    echo "$default"
  else
    echo "$reply"
  fi
}

set_env_if_placeholder() {
  local file="$1"
  local key="$2"
  local new_value="$3"
  local current=""
  current="$(env_get_file "$file" "$key")"

  case "$current" in
    ""|change-me*|change_me*|n8n-local-*)
      upsert_env "$file" "$key" "$new_value"
      log "Set $key"
      ;;
    *)
      log "Keeping existing $key"
      ;;
  esac
}

set_env_if_missing() {
  local file="$1"
  local key="$2"
  local value="$3"
  if ! grep -q "^${key}=" "$file" 2>/dev/null; then
    upsert_env "$file" "$key" "$value"
    log "Set $key"
  fi
}

setup_rag_profile() {
  upsert_env .env LIGHTRAG_MCP_ENABLED 1
  log "Enabled LightRAG MCP registration (LIGHTRAG_MCP_ENABLED=1)"
}

setup_neo4j_profile() {
  upsert_env .env NEO4J_ENABLED 1
  log "Enabled Neo4j (NEO4J_ENABLED=1)"
}

setup_calendar_profile() {
  upsert_env .env CALDAV_MCP_ENABLED 1
  log "Enabled CalDAV MCP registration (CALDAV_MCP_ENABLED=1)"
  log "After make up, connect each account: scripts/addon.sh connect caldav --instance <slug> < account.json (docs/calendar.md)"
}

# Shared Postgres + SQL MCP. The secrets are generated for every install (see
# Secrets below), so this only turns the profile and the registration on.
setup_db_profile() {
  upsert_env .env SQL_MCP_ENABLED 1
  local db_dir
  db_dir="$(resolve_data_root_path "$(data_root_env_get DB_DATA_DIR ./data/db)")"
  mkdir -p "$db_dir"
  log "Enabled shared Postgres (data: ${db_dir}) and SQL MCP registration (SQL_MCP_ENABLED=1)"
  log "Register an integration's schema: make db-service-add NAME=<slug>. See docs/database.md"
}

# Vikunja todo lists. Its profile is in x-db-profiles, so the shared Postgres
# starts with it; SQL MCP registration stays with --db. Tokens need the stack
# up, so they come from make todo-init afterwards.
setup_todo_profile() {
  ./scripts/todo.sh prepare
  upsert_env .env VIKUNJA_MCP_ENABLED 1
  log "Enabled Vikunja (todo profile). After make up: make todo-init (account + MCP tokens). See docs/todo.md"
}

# Draft-only mail MCP. Requests queue in MAIL_DRAFTS_DIR; make mail-drafts (or
# mail-drafts-watch) on the host saves them to Mail's Drafts. Nothing sends.
setup_mail_drafts_profile() {
  upsert_env .env MAIL_DRAFTS_ENABLED 1
  local drafts_dir
  drafts_dir="$(resolve_data_root_path "$(data_root_env_get MAIL_DRAFTS_DIR ./data/mail-drafts)")"
  mkdir -p "$drafts_dir"
  chmod 700 "$drafts_dir"
  log "Enabled mail drafts (profile mail-drafts, folder ${drafts_dir}). Drafts are never sent for you"
  log "Then: make up, re-run both hermes-*-bootstrap services, and run make mail-drafts-watch on the host. See docs/email.md"
}

# Read-only Messages MCP. The index is built on the host by the Messages
# reader app (or make messages-index), which needs Full Disk Access and a
# filled-in messages-index.toml; nothing here touches the Messages folder. The
# watcher writes new-message events to MESSAGES_EVENTS_DIR, and messages-mcp
# forwards them to the default profile's webhook route, signed with
# MESSAGES_WEBHOOK_SECRET.
setup_messages_profile() {
  upsert_env .env MESSAGES_MCP_ENABLED 1
  set_env_if_placeholder .env MESSAGES_WEBHOOK_SECRET "$(rand_hex 32)"
  local index_dir events_dir
  index_dir="$(resolve_data_root_path "$(data_root_env_get MESSAGES_INDEX_DIR ./data/messages)")"
  events_dir="$(resolve_data_root_path "$(data_root_env_get MESSAGES_EVENTS_DIR ./data/messages-events)")"
  mkdir -p "$index_dir" "$events_dir"
  chmod 700 "$index_dir" "$events_dir"
  if [[ ! -f messages-index.toml ]]; then
    {
      sed -e "s|^index = .*|index = \"${index_dir}/index.sqlite\"|" imessage/messages-index.toml.example
      printf '\n# Added by setup.sh --messages: events go to the folder messages-mcp forwards from.\n'
      printf '[notify]\nchats = ["*;-;*"]\noutbox = "%s"\n' "$events_dir"
    } > messages-index.toml
    log "Created messages-index.toml — set store, since and chats (make messages-index-chats lists them)"
  fi
  log "Enabled the Messages MCP (profile messages, index ${index_dir}, events ${events_dir})"
  log "Then: build and install the reader app, grant it Full Disk Access, make up, make hermes-reload, make messages-triage-cron. See docs/messages.md"
}

# Read-only mail MCP. The index is built on the host by make mail-index, which
# needs a filled-in mail-index.toml; nothing here touches the Mail store.
setup_mail_profile() {
  upsert_env .env MAIL_MCP_ENABLED 1
  local index_dir
  index_dir="$(resolve_data_root_path "$(data_root_env_get MAIL_INDEX_DIR ./data/mail)")"
  mkdir -p "$index_dir"
  chmod 700 "$index_dir"
  if [[ ! -f mail-index.toml ]]; then
    cp mail-index.toml.example mail-index.toml
    log "Created mail-index.toml — set since and accounts (make mail-index-mailboxes lists them; macOS asks to let it control Mail)"
  fi
  log "Enabled the mail MCP (profile mail, index folder ${index_dir})"
  log "Then: make mail-index-doctor && make mail-index && make up, and re-run both hermes-*-bootstrap services. See docs/email.md"
}

setup_automation_profile() {
  upsert_env .env AUTOMATION_ENABLED 1
}

# gptr-mcp alone, registered as Hermes tools on the dashboard and WebUI
# profiles. The REST service and n8n stay with --automation.
# Local Twenty CRM. Workspaces and Hermes keys come from make crm-init after
# make up; remote instances are added with make crm-add instead.
setup_crm_profile() {
  upsert_env .env TWENTY_LOCAL_ENABLED 1
  upsert_env .env TWENTY_MCP_ENABLED 1
  keep_stored_local_setting twenty
  log "Enabled Twenty CRM (crm profile). After make up: make crm-init. See docs/crm.md"
}

# keep_stored_local_setting SHORT_ID: an add-on's local setting owns its
# *_LOCAL_ENABLED flag once stored (addon.sh configure), and addon.sh enable
# applies it. When one is stored, set it to 1 as well, so the next enable
# doesn't turn the local instance off again.
keep_stored_local_setting() {
  local id="$1" settings key
  key="ADDON_$(printf '%s' "$id" | tr '[:lower:]-' '[:upper:]_')_LOCAL"
  settings="$(resolve_data_root_path)/addons/${id}/config/settings.env"
  if [[ -f "$settings" ]] && grep -q "^${key}=" "$settings"; then
    upsert_env "$settings" "$key" 1
  fi
}

# Local Actual Budget server and its MCP servers. The server password and a
# first budget come from the add-on's init hook (scripts/addon.sh enable
# actual) after make up; budgets are connected with make budget-connect.
setup_budget_profile() {
  upsert_env .env ACTUAL_LOCAL_ENABLED 1
  upsert_env .env ACTUAL_MCP_ENABLED 1
  keep_stored_local_setting actual
  log "Enabled Actual Budget (budget-local and budget profiles). After make up: scripts/addon.sh enable actual, then make budget-connect. See docs/budget.md"
}

# Twenty reads true/false; the stack's flag is 0/1 like every other.
sync_twenty_multiworkspace_bool() {
  local value
  value="$(env_get_file .env TWENTY_MULTIWORKSPACE)"
  case "$value" in
    1|true|TRUE|yes|YES) upsert_env .env TWENTY_MULTIWORKSPACE_BOOL true ;;
    *) upsert_env .env TWENTY_MULTIWORKSPACE_BOOL false ;;
  esac
}

setup_research_profile() {
  upsert_env .env GPTR_MCP_ENABLED 1
  log "Enabled GPT Researcher MCP registration (GPTR_MCP_ENABLED=1). Then: make up. See docs/gpt-researcher.md"
}

setup_coding_profile() {
  upsert_env .env OPENCODE_MCP_ENABLED 1
  set_env_if_missing .env CODING_EXTRA_ROOTS ""
  set_env_if_missing .env CONTEXT_EXTRA_ROOTS ""
  log "Enabled OpenCode MCP registration (OPENCODE_MCP_ENABLED=1)"
}

# Zero-config demo path: Ollama in Docker + small chat/embed models.
# Rewrites LLM_* / EMBEDDING_* to the in-compose ollama service.
setup_ollama_profile() {
  upsert_env .env OLLAMA_ENABLED 1
  set_env_if_missing .env OLLAMA_CHAT_MODEL qwen2.5:7b-instruct
  set_env_if_missing .env OLLAMA_EMBED_MODEL nomic-embed-text
  set_env_if_missing .env OLLAMA_HOST_PORT 11434

  upsert_env .env LLM_BINDING ollama
  upsert_env .env LLM_MODEL qwen2.5:7b-instruct
  upsert_env .env LLM_BINDING_HOST http://ollama:11434
  upsert_env .env LLM_BINDING_API_KEY ""

  upsert_env .env LIGHTRAG_LLM_BINDING ollama
  upsert_env .env LIGHTRAG_LLM_MODEL qwen2.5:7b-instruct
  upsert_env .env LIGHTRAG_LLM_BINDING_HOST http://ollama:11434
  upsert_env .env LIGHTRAG_LLM_BINDING_API_KEY ""

  upsert_env .env EMBEDDING_BINDING ollama
  upsert_env .env EMBEDDING_MODEL nomic-embed-text
  upsert_env .env EMBEDDING_DIM 768
  upsert_env .env EMBEDDING_BINDING_HOST http://ollama:11434
  upsert_env .env EMBEDDING_BINDING_API_KEY ""

  # Native Ollama has no /v1, so LightRAG talks to it directly. Hermes,
  # OpenCode, and GPT Researcher still use the proxy, which adds /v1.
  upsert_env .env LIGHTRAG_LLM_CLIENT_HOST http://ollama:11434
  upsert_env .env EMBEDDING_CLIENT_HOST http://ollama:11434

  log "Pointed LLM/embedding bindings at in-compose Ollama (http://ollama:11434)"
}

# ed25519 key for terminal.backend ssh. The private key is mounted only into
# hermes. The public key is the worker's authorized_keys. Not a secret of the
# host .env, and never written into the worker environment.
ensure_hermes_worker_key() {
  local dir="$ROOT/compose/hermes/worker/ssh"
  local key="$dir/id_ed25519"
  mkdir -p "$dir"
  if [[ ! -f "$key" ]]; then
    ssh-keygen -t ed25519 -f "$key" -N "" -C hermes-worker
    log "Created hermes-worker SSH key"
  else
    log "Keeping existing hermes-worker SSH key"
  fi
  chmod 600 "$key"
  chmod 644 "${key}.pub"
}

# --- Preflight ---
require_cmd docker
require_cmd ssh-keygen
docker compose version >/dev/null 2>&1 || die "docker compose is not available"

if ! docker info >/dev/null 2>&1; then
  die "Docker daemon is not running"
fi

# --- Workspace ---
if [[ -z "$WORKSPACE" ]]; then
  if [[ -f .env ]] && grep -q '^WORKSPACE=' .env; then
    WORKSPACE="$(env_get_file .env WORKSPACE)"
  else
    WORKSPACE="$(prompt_default "Knowledge-base slug (WORKSPACE)" "my-kb")"
  fi
fi
validate_workspace "$WORKSPACE"

if [[ -z "$WEBUI_TITLE" ]]; then
  if [[ -f .env ]] && grep -q '^WEBUI_TITLE=' .env; then
    WEBUI_TITLE="$(env_get_file .env WEBUI_TITLE)"
  else
    WEBUI_TITLE="$(prompt_default "Web UI title (WEBUI_TITLE)" "My Knowledge Base")"
  fi
fi

# --- .env ---
if [[ ! -f .env ]]; then
  cp .env.example .env
  log "Created .env from .env.example"
  # On Linux the Hermes services run as HERMES_UID:HERMES_GID over bind mounts
  # this user creates, so the two must be this user's ids. Docker Desktop maps
  # ownership and keeps the example's 1000. An existing .env is left alone.
  if [[ "$(uname -s)" == Linux ]]; then
    upsert_env .env HERMES_UID "$(id -u)"
    upsert_env .env HERMES_GID "$(id -g)"
    log "Set HERMES_UID=$(id -u) and HERMES_GID=$(id -g) for this user"
  fi
else
  log "Using existing .env"
fi

upsert_env .env WORKSPACE "$WORKSPACE"
upsert_env .env WEBUI_TITLE "'${WEBUI_TITLE}'"

# --- Secrets ---
set_env_if_placeholder .env NEO4J_PASSWORD "$(rand_secret)"
set_env_if_placeholder .env POSTGRES_PASSWORD "$(rand_secret)"
set_env_if_placeholder .env OPENCODE_SERVER_PASSWORD "$(rand_secret)"
set_env_if_placeholder .env LIGHTRAG_API_KEY "lr_$(rand_hex 16)"
set_env_if_placeholder .env N8N_ENCRYPTION_KEY "$(rand_hex 16)"
set_env_if_placeholder .env N8N_USER_MANAGEMENT_JWT_SECRET "$(rand_hex 16)"
# Hermes dashboard basic auth, mandatory since agent 0.20.0. The dashboard binds
# 0.0.0.0 inside the container and refuses to start without an auth provider,
# which takes the hermes healthcheck down with it.
set_env_if_placeholder .env HERMES_DASHBOARD_PASSWORD "$(rand_hex 16)"
set_env_if_placeholder .env HERMES_DASHBOARD_AUTH_SECRET "$(rand_hex 32)"
set_env_if_placeholder .env HERMES_DASHBOARD_HANDOFF_SECRET "$(rand_hex 32)"
# Client gateway bearer token (profile client-gateway), generated for every
# install so enabling the profile needs no extra step.
set_env_if_placeholder .env CLIENT_API_TOKEN "$(rand_hex 32)"
set_env_if_placeholder .env LLM_PROXY_ADMIN_TOKEN "$(rand_hex 32)"
# Shared Postgres (db profile). Hex, so they drop into connection URLs as-is.
# Generated whether or not db is on, like POSTGRES_PASSWORD for n8n.
set_env_if_placeholder .env DB_ADMIN_PASSWORD "$(rand_hex 24)"
set_env_if_placeholder .env DB_AGENT_RO_PASSWORD "$(rand_hex 24)"
set_env_if_placeholder .env DB_AGENT_RW_PASSWORD "$(rand_hex 24)"
# Vikunja session signing key (todo profile). Generated for every install so
# enabling todo later needs no extra step.
set_env_if_placeholder .env VIKUNJA_SECRET "$(rand_hex 32)"
# Twenty (crm profile), generated for every install like VIKUNJA_SECRET.
# ENCRYPTION_KEY protects secrets stored in Twenty: never replaced once set.
# The database password is hex because it goes unencoded into PG_DATABASE_URL.
# Skipped when the secret comes from a file: a value as well would stop the
# containers at start.
if ! grep -qE '^TWENTY_ENCRYPTION_KEY_FILE=.+' .env 2>/dev/null; then
  set_env_if_placeholder .env TWENTY_ENCRYPTION_KEY "$(random_value base64 32)"
fi
if ! grep -qE '^TWENTY_DB_PASSWORD_FILE=.+' .env 2>/dev/null; then
  set_env_if_placeholder .env TWENTY_DB_PASSWORD "$(rand_hex 32)"
fi

# Rewritten on every run from the tracked template; up.sh only creates it.
SEARXNG_SECRET="$(searxng_secret)"
sync_searxng_secret "$SEARXNG_SECRET"

# --- OpenCode workspace host ---
if [[ -z "$OPENCODE_WORKSPACE_HOST" ]]; then
  if grep -q '^OPENCODE_WORKSPACE_HOST=' .env; then
    OPENCODE_WORKSPACE_HOST="$(env_get_file .env OPENCODE_WORKSPACE_HOST)"
  fi
  if [[ -z "$OPENCODE_WORKSPACE_HOST" || "$OPENCODE_WORKSPACE_HOST" == /Users/you/* ]]; then
    default_opencode="${HOME}/code"
    OPENCODE_WORKSPACE_HOST="$(prompt_default "OpenCode host code path (OPENCODE_WORKSPACE_HOST)" "$default_opencode")"
  fi
fi
validate_opencode_workspace "$OPENCODE_WORKSPACE_HOST"
mkdir -p "$OPENCODE_WORKSPACE_HOST"
upsert_env .env OPENCODE_WORKSPACE_HOST "$OPENCODE_WORKSPACE_HOST"

# --- ASSISTANT_DATA_ROOT (user content) + ./data/hermes (agent state) ---
# User trees are bind-mounted into Hermes/LightRAG. Create them here; if Docker
# gets there first it creates them owned by root. Hermes state stays under the
# repo's data/hermes regardless of ASSISTANT_DATA_ROOT.
if [[ -n "$ASSISTANT_DATA_ROOT_ARG" ]]; then
  DATA_ROOT="$(expand_user_path "$ASSISTANT_DATA_ROOT_ARG")"
  mkdir -p "$DATA_ROOT"
  DATA_ROOT="$(resolve_data_root_path "$DATA_ROOT")"
else
  existing="$(data_root_env_get ASSISTANT_DATA_ROOT "")"
  if [[ -n "$existing" && "$existing" != "./data" ]]; then
    DATA_ROOT="$(resolve_data_root_path "$existing")"
  else
    DATA_ROOT="${ROOT}/data"
  fi
fi
validate_data_root_path "$DATA_ROOT" || die "Invalid ASSISTANT_DATA_ROOT: $DATA_ROOT"
# Prefer absolute path in .env when outside the default repo-relative ./data
if [[ "$DATA_ROOT" == "${ROOT}/data" ]]; then
  upsert_env .env ASSISTANT_DATA_ROOT "./data"
else
  upsert_env .env ASSISTANT_DATA_ROOT "$DATA_ROOT"
fi

seed_data_root "$DATA_ROOT"
mkdir -p "${DATA_ROOT}/inputs/${WORKSPACE}" "${DATA_ROOT}/rag_storage/${WORKSPACE}" "data/hermes/memories"
seed_file "${SEED_DIR}/getting-started.md" "${DATA_ROOT}/inputs/${WORKSPACE}/getting-started.md" "WORKSPACE=${WORKSPACE}"
seed_file "${SEED_DIR}/hermes-USER.md" "data/hermes/memories/USER.md"
log "Created ${DATA_ROOT}/{inputs,rag_storage,voice,projects,memory,corpora} and data/hermes/"

# --- Default profile: core (Hermes + WebUI + SearXNG) ---
if ! grep -q '^COMPOSE_PROFILES=' .env 2>/dev/null; then
  upsert_env .env COMPOSE_PROFILES core
  log "Set COMPOSE_PROFILES=core"
elif [[ -z "$(env_get_file .env COMPOSE_PROFILES)" ]]; then
  upsert_env .env COMPOSE_PROFILES core
  log "Set COMPOSE_PROFILES=core"
else
  # Ensure core is present, replacing the pre-core hermes and hermes-webui names.
  current="$(env_get_file .env COMPOSE_PROFILES | tr ',' '\n' | grep -vx -e hermes -e hermes-webui | sed '/^$/d' | paste -sd, - || true)"
  if [[ -z "$current" ]]; then
    current=core
  elif [[ ",${current}," != *",core,"* ]]; then
    current="${current},core"
  fi
  upsert_env .env COMPOSE_PROFILES "$current"
fi

# Hermes env files are required bind-mount sources for the core profile.
ensure_hermes_env_files
ensure_hermes_worker_key
ensure_opencode_local_config
# The browser service's token files are a bind-mount source when its profile is on.
./scripts/web-browser-secrets.sh

# LightRAG MCP stays off until rag is enabled (avoids connect timeouts on core-only).
set_env_if_missing .env LIGHTRAG_MCP_ENABLED 0

# CalDAV MCP stays off until calendar is enabled.
set_env_if_missing .env CALDAV_MCP_ENABLED 0

# SQL MCP stays off until db is enabled. The services dir is a bind-mount
# source for db-provision; create it before Docker does (root-owned on Linux).
set_env_if_missing .env SQL_MCP_ENABLED 0
mkdir -p compose/postgres/services

# Vikunja MCP stays off until todo is enabled and make todo-init has run.
set_env_if_missing .env VIKUNJA_MCP_ENABLED 0

# Every add-on's store under <data root>/addons, created now so core
# services' bind mounts find it, whether or not the add-on is enabled.
./scripts/addon-dirs.sh

# The Bitwarden broker's API token. hermes mounts its folder on the core
# profile, so the file has to exist before compose reads the mount.
./scripts/vault.sh api-token

# Twenty CRM stays off until crm is enabled (local) or a remote is added.
set_env_if_missing .env TWENTY_LOCAL_ENABLED 0
set_env_if_missing .env TWENTY_MCP_ENABLED 0
set_env_if_missing .env TWENTY_MULTIWORKSPACE 0

# Actual Budget stays off until budget is enabled (local) or a remote is added.
set_env_if_missing .env ACTUAL_LOCAL_ENABLED 0
set_env_if_missing .env ACTUAL_MCP_ENABLED 0

# Mail MCP stays off until mail is enabled.
set_env_if_missing .env MAIL_MCP_ENABLED 0
set_env_if_missing .env MAIL_DRAFTS_ENABLED 0
set_env_if_missing .env MESSAGES_MCP_ENABLED 0

# OpenCode is part of the default stack: a missing flag is written as 1. An
# install that set it to 0 keeps it off.
set_env_if_missing .env OPENCODE_MCP_ENABLED 1
set_env_if_missing .env CODING_EXTRA_ROOTS ""
set_env_if_missing .env CONTEXT_EXTRA_ROOTS ""
set_env_if_missing .env HERMES_TERMINAL_BACKEND ssh

# Signal daemon off by default; sync profile + data/hermes/.env from the toggle.
set_env_if_missing .env HERMES_SIGNAL_ENABLED 0
set_env_if_missing .env SIGNAL_CLI_IMAGE "registry.gitlab.com/packaging/signal-cli/signal-cli-jre:v0-14-10-1"
./scripts/sync-signal-profile.sh

# Buzz off by default; independent agents are operator-local under data/hermes/.
set_env_if_missing .env HERMES_BUZZ_ENABLED 0
./scripts/sync-buzz-profile.sh
./scripts/sync-terminal-backend.sh

# --- Optional profiles ---
if [[ "$SETUP_RAG" -eq 1 ]]; then
  setup_rag_profile
fi
if [[ "$SETUP_NEO4J" -eq 1 ]]; then
  setup_neo4j_profile
fi
if [[ "$SETUP_AUTOMATION" -eq 1 ]]; then
  setup_automation_profile
fi
if [[ "$SETUP_RESEARCH" -eq 1 ]]; then
  setup_research_profile
fi
if [[ "$SETUP_CODING" -eq 1 ]]; then
  setup_coding_profile
fi
if [[ "$SETUP_CALENDAR" -eq 1 ]]; then
  setup_calendar_profile
fi
if [[ "$SETUP_DB" -eq 1 ]]; then
  setup_db_profile
fi
if [[ "$SETUP_TODO" -eq 1 ]]; then
  setup_todo_profile
fi
if [[ "$SETUP_CRM" -eq 1 ]]; then
  setup_crm_profile
fi
if [[ "$SETUP_BUDGET" -eq 1 ]]; then
  setup_budget_profile
fi
sync_twenty_multiworkspace_bool
if [[ "$SETUP_MAIL_DRAFTS" -eq 1 ]]; then
  setup_mail_drafts_profile
fi
if [[ "$SETUP_MESSAGES" -eq 1 ]]; then
  setup_messages_profile
fi
if [[ "$SETUP_MAIL" -eq 1 ]]; then
  setup_mail_profile
fi
if [[ "$SETUP_OLLAMA" -eq 1 ]]; then
  setup_ollama_profile
fi

# COMPOSE_PROFILES follows the *_ENABLED flags from here on (README.md#profiles).
./scripts/sync-profiles.sh

PROFILES_NOW="$(env_get_file .env COMPOSE_PROFILES core)"

cat <<EOF

Setup complete.

  Workspace:  ${WORKSPACE}
  Profiles:   ${PROFILES_NOW}
  Data root:  ${DATA_ROOT}
  Inputs:     ${DATA_ROOT}/inputs/${WORKSPACE}/
  Hermes:     ${ROOT}/data/hermes/ (agent state; not relocated)
  OpenCode:   ${OPENCODE_WORKSPACE_HOST} (same path inside the container)

Next steps:
  1. Point .env at a model server (OpenAI-compatible host API / host Ollama), OR use --ollama.
  2. make doctor
  3. make up
  4. Open Hermes WebUI: http://localhost:8787
     Dashboard:         http://localhost:9119

Optional profiles (re-run setup with a flag below, or set the service's
*_ENABLED flag in .env and run make up; make services lists them):
  --rag           Knowledge graph (LightRAG + Neo4j)
  --neo4j         Neo4j alone, without LightRAG
  --automation    n8n + GPT Researcher
  --research      GPT Researcher deep research tools for Hermes, without n8n (docs/gpt-researcher.md)
  --coding        OpenCode
  --calendar      CalDAV calendars (iCloud, Nextcloud, …) via caldav-mcp
  --db            Shared Postgres + SQL MCP for Hermes (docs/database.md)
  --todo          Vikunja todo lists + MCP, then make todo-init (docs/todo.md)
  --crm           Local Twenty CRM + MCP, then make crm-init (docs/crm.md)
  --budget        Local Actual Budget + MCP, then make budget-connect (docs/budget.md)
  --mail          Read-only search over your Apple Mail, indexed on the host (docs/email.md)
  --mail-drafts   Hermes drafts email into Mail's Drafts for you to send (docs/email.md)
  --messages      Read-only iMessage/SMS history and new-message alerts, indexed on the host (docs/messages.md)
  --ollama        Bundled Ollama + demo model pull

Signal (optional): set HERMES_SIGNAL_ENABLED=1 and SIGNAL_ACCOUNT in .env,
link signal-cli on the host, then re-run setup or make ensure-local. See docs/signal.md.

Buzz (optional): set HERMES_BUZZ_ENABLED=1 and BUZZ_RELAY_URL, install the CLI
with make bootstrap-buzz, create agents with make agent-create NAME=<slug> WITH=buzz.
Profiles stay under data/hermes/ (not git). See docs/buzz.md.

EOF

if [[ "$SETUP_OLLAMA" -eq 1 ]]; then
  cat <<'EOF'
Ollama profile enabled:
  After `make up`, pull demo models once:
    docker compose run --rm ollama-pull
  Models default to qwen2.5:7b-instruct + nomic-embed-text.

EOF
fi



cat <<'EOF'
Next: make up. The first start seeds Hermes config from .env (LLM_MODEL).

EOF
