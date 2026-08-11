#!/usr/bin/env bash
# Idempotent first-time setup: workspace, data/ tree, secrets, n8n credentials.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WORKSPACE=""
WEBUI_TITLE=""
OPENCODE_WORKSPACE_HOST=""
SETUP_OLLAMA=0
SETUP_RAG=0
SETUP_AUTOMATION=0
SETUP_CODING=0
NON_INTERACTIVE=0

usage() {
  cat <<'EOF'
Usage: ./scripts/setup.sh [options]

Options:
  --workspace SLUG          Knowledge-base slug (alphanumeric + hyphens)
  --title TITLE             LightRAG web UI title
  --opencode-workspace PATH Host path for OpenCode workspace mount
  --rag                     Enable knowledge graph (compose profile rag)
  --automation              Enable n8n + GPT Researcher (profile automation)
  --coding                  Enable OpenCode (profile coding)
  --ollama                  Bundle Ollama in Docker + demo model pulls (profile ollama)
  --hermes                  Deprecated alias; core already includes Hermes
  --non-interactive         Fail if required values are missing
  -h, --help                Show this help

Default COMPOSE_PROFILES=core (Hermes + WebUI + SearXNG).
Examples:
  ./scripts/setup.sh
  ./scripts/setup.sh --rag --automation --coding
  ./scripts/setup.sh --ollama --non-interactive --workspace my-kb --title "My KB"
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --title) WEBUI_TITLE="$2"; shift 2 ;;
    --opencode-workspace) OPENCODE_WORKSPACE_HOST="$2"; shift 2 ;;
    --hermes) shift ;; # deprecated alias; core already includes Hermes
    --rag) SETUP_RAG=1; shift ;;
    --automation) SETUP_AUTOMATION=1; shift ;;
    --coding) SETUP_CODING=1; shift ;;
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
  openssl rand -hex "$1"
}

rand_secret() {
  openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 32
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

upsert_env() {
  local file="$1"
  local key="$2"
  local value="$3"
  touch "$file"
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    local tmp
    tmp="$(mktemp)"
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" == "${key}="* ]]; then
        printf '%s=%s\n' "$key" "$value"
      else
        printf '%s\n' "$line"
      fi
    done < "$file" > "$tmp"
    mv "$tmp" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

set_env_if_placeholder() {
  local file="$1"
  local key="$2"
  local new_value="$3"
  local current=""
  current="$(grep -E "^${key}=" "$file" 2>/dev/null | head -1 | cut -d= -f2- || true)"
  current="${current%\"}"
  current="${current#\"}"
  current="${current#\'}"
  current="${current%\'}"

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

append_compose_profile() {
  local profile="$1"
  local current=""
  current="$(grep '^COMPOSE_PROFILES=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)"
  current="${current%\"}"
  current="${current#\"}"
  # Migrate pre-core profile names to `core`.
  current="${current//hermes-webui/}"
  current="${current//hermes/}"
  current="$(echo "$current" | tr ',' '\n' | sed '/^$/d' | paste -sd, -)"
  if [[ -z "$current" ]]; then
    upsert_env .env COMPOSE_PROFILES "$profile"
    log "Set COMPOSE_PROFILES=$profile"
  elif [[ ",${current}," != *",${profile},"* ]]; then
    upsert_env .env COMPOSE_PROFILES "${current},${profile}"
    log "Appended $profile to COMPOSE_PROFILES"
  else
    upsert_env .env COMPOSE_PROFILES "$current"
    log "COMPOSE_PROFILES already includes $profile"
  fi
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

# Write opencode/opencode.local.json (gitignored) and leave tracked
# opencode.json as a placeholder template (provider "local", model
# "your-chat-model"). Real model ids and baseURL belong in the local overlay
# only. Compose mounts that overlay when it exists.
ensure_opencode_local_config() {
  local base="$ROOT/opencode/opencode.json"
  local local_cfg="$ROOT/opencode/opencode.local.json"
  [[ -f "$base" ]] || die "Missing $base"
  if [[ ! -f "$local_cfg" ]]; then
    cp "$base" "$local_cfg"
    log "Created opencode/opencode.local.json from opencode.json (edit provider/model for your server)"
  fi
}

setup_rag_profile() {
  append_compose_profile rag
  upsert_env .env LIGHTRAG_MCP_ENABLED 1
  log "Enabled LightRAG MCP registration (LIGHTRAG_MCP_ENABLED=1)"
}

setup_automation_profile() {
  append_compose_profile automation
}

setup_coding_profile() {
  append_compose_profile coding
  ensure_opencode_local_config
}

# Zero-config demo path: Ollama in Docker + small chat/embed models.
# Rewrites LLM_* / EMBEDDING_* to the in-compose ollama service.
setup_ollama_profile() {
  append_compose_profile ollama
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

  upsert_env .env OPENAI_BASE_URL http://ollama:11434/v1
  upsert_env .env OPENAI_API_KEY ollama
  upsert_env .env FAST_LLM openai:qwen2.5:7b-instruct
  upsert_env .env SMART_LLM openai:qwen2.5:7b-instruct
  upsert_env .env STRATEGIC_LLM openai:qwen2.5:7b-instruct
  upsert_env .env EMBEDDING openai:nomic-embed-text

  log "Pointed LLM/embedding bindings at in-compose Ollama (http://ollama:11434)"
}

ensure_hermes_env_files() {
  local hermes_env="$ROOT/compose/hermes/api-server.env"
  local browser_env="$ROOT/compose/hermes/browser.env"
  if [[ ! -f "$hermes_env" ]]; then
    cp compose/hermes/api-server.env.example "$hermes_env"
    upsert_env "$hermes_env" API_SERVER_KEY "$(rand_hex 32)"
    log "Created compose/hermes/api-server.env"
  else
    log "Keeping existing compose/hermes/api-server.env"
  fi
  if [[ ! -f "$browser_env" ]]; then
    cp compose/hermes/browser.env.example "$browser_env"
    upsert_env "$browser_env" API_SERVER_KEY "$(rand_hex 32)"
    log "Created compose/hermes/browser.env"
  else
    log "Keeping existing compose/hermes/browser.env"
  fi
}

# Write searxng/settings.local.yml (gitignored) from the tracked template.
# Compose mounts the local file, which keeps setup from ever touching the
# tracked settings.yml.
sync_searxng_secret() {
  local secret="$1"
  local template="$ROOT/searxng/settings.yml"
  local settings="$ROOT/searxng/settings.local.yml"
  [[ -f "$template" ]] || die "Missing $template"
  local tmp
  tmp="$(mktemp)"
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^[[:space:]]*secret_key:[[:space:]]* ]]; then
      printf '  secret_key: "%s"\n' "$secret"
    else
      printf '%s\n' "$line"
    fi
  done < "$template" > "$tmp"
  mv "$tmp" "$settings"
  log "Synced SEARXNG_SECRET into searxng/settings.local.yml"
}

create_getting_started() {
  local workspace="$1"
  local dest="$ROOT/data/inputs/${workspace}/getting-started.md"
  cat > "$dest" <<EOF
# Getting started

Welcome to your local knowledge base (**${workspace}**).

Drop documents into this folder — PDF, DOCX, MD, TXT, and more — then either:

1. Upload via the LightRAG web UI at http://localhost:9621/webui/, or
2. Trigger a scan from the **LightRAG Scan Inputs** n8n workflow.

After ingestion, explore the knowledge graph in the LightRAG UI or Neo4j Browser.
EOF
  log "Wrote $dest"
}

create_voice_readme() {
  local dest="$ROOT/data/voice/README.md"
  cat > "$dest" <<'EOF'
# Writing voices

Each subdirectory here is one writing voice. Hermes reads them at `/opt/voice`.
Nothing in this directory is tracked by git.

## Setup

1. Create a voice and drop in samples:

       mkdir -p data/voice/my-voice/samples
       cp ~/writing/*.md data/voice/my-voice/samples/

   Use finished writing you would be happy to publish again. Whatever lands
   here gets imitated, warts and all. Aim for 5 or more pieces of 500+ words.
   Plain text and markdown are read (.md, .txt, .rst).

2. Calibrate once per voice, and again whenever you add samples. In Hermes:

       Calibrate the my-voice writing voice.

   This writes `data/voice/my-voice/STYLE.md`. Read it. It is a normal markdown
   file, and editing it by hand improves results faster than anything else.
   The "Never does" section deserves the most attention.

3. Write:

       Write a 600-word post about pricing in the my-voice voice.

## Layout

    data/voice/
      my-voice/
        samples/      # your writing (you add these)
        STYLE.md      # produced by calibration; hand-editable
        drafts/       # generated output

## Notes

- Requires the Hermes profile. For the full step-by-step guide, including how to
  pick a corpus and troubleshoot, see `docs/writing-voice.md`.
- Style matching is one of the harder tasks for a small local model. When drafts
  feel flat, try a larger drafting model before adding samples.
EOF
  log "Wrote $dest"
}

create_projects_readme() {
  local dest="$ROOT/data/projects/README.md"
  cat > "$dest" <<'EOF'
# Projects

Each subdirectory here is one project: a working directory the agent reads from
and drafts into. Hermes reads them at `/opt/projects`. Git tracks none of it.

Use this for work that needs its own reference material and generated output
side by side, like a job search, a book, a client engagement, or a research topic.

## Setup

Create a project and give it whatever structure fits the work:

    mkdir -p data/projects/my-project
    cp ~/reference/*.md data/projects/my-project/

Then name it when you ask Hermes to do something:

    Using the notes in /opt/projects/my-project, draft a summary of X.

The mount is live, so files you add appear immediately. No Hermes restart
required.

## Keep source material and generated output apart

Put reference material you wrote and drafts the agent produced in separate
subdirectories. Mix them and the corpus starts citing its own output back to
you as fact. Keeping them apart also lets you ingest only the source half into
LightRAG later, once a project outgrows reading files directly.

## Notes

- Requires the Hermes profile.
- Agents can write here. Review generated files before treating them as fact.
EOF
  log "Wrote $dest"
}

# --- Preflight ---
require_cmd docker
require_cmd openssl
docker compose version >/dev/null 2>&1 || die "docker compose is not available"
require_cmd node

if ! docker info >/dev/null 2>&1; then
  die "Docker daemon is not running"
fi

# --- Workspace ---
if [[ -z "$WORKSPACE" ]]; then
  if [[ -f .env ]] && grep -q '^WORKSPACE=' .env; then
    WORKSPACE="$(grep '^WORKSPACE=' .env | head -1 | cut -d= -f2-)"
    WORKSPACE="${WORKSPACE%\"}"
    WORKSPACE="${WORKSPACE#\"}"
    WORKSPACE="${WORKSPACE#\'}"
    WORKSPACE="${WORKSPACE%\'}"
  else
    WORKSPACE="$(prompt_default "Knowledge-base slug (WORKSPACE)" "my-kb")"
  fi
fi
validate_workspace "$WORKSPACE"

if [[ -z "$WEBUI_TITLE" ]]; then
  if [[ -f .env ]] && grep -q '^WEBUI_TITLE=' .env; then
    WEBUI_TITLE="$(grep '^WEBUI_TITLE=' .env | head -1 | cut -d= -f2- | tr -d "'\"")"
  else
    WEBUI_TITLE="$(prompt_default "Web UI title (WEBUI_TITLE)" "My Knowledge Base")"
  fi
fi

# --- .env ---
if [[ ! -f .env ]]; then
  cp .env.example .env
  log "Created .env from .env.example"
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

SEARXNG_SECRET="$(grep '^SEARXNG_SECRET=' .env | head -1 | cut -d= -f2- || true)"
if [[ -z "$SEARXNG_SECRET" || "$SEARXNG_SECRET" == change-me* ]]; then
  SEARXNG_SECRET="$(rand_hex 24)"
  upsert_env .env SEARXNG_SECRET "$SEARXNG_SECRET"
  log "Set SEARXNG_SECRET"
fi
sync_searxng_secret "$SEARXNG_SECRET"

# --- OpenCode workspace host ---
if [[ -z "$OPENCODE_WORKSPACE_HOST" ]]; then
  if grep -q '^OPENCODE_WORKSPACE_HOST=' .env; then
    OPENCODE_WORKSPACE_HOST="$(grep '^OPENCODE_WORKSPACE_HOST=' .env | head -1 | cut -d= -f2-)"
    OPENCODE_WORKSPACE_HOST="${OPENCODE_WORKSPACE_HOST%\"}"
    OPENCODE_WORKSPACE_HOST="${OPENCODE_WORKSPACE_HOST#\"}"
  fi
  if [[ -z "$OPENCODE_WORKSPACE_HOST" || "$OPENCODE_WORKSPACE_HOST" == /Users/you/* ]]; then
    default_opencode="${HOME}/code"
    OPENCODE_WORKSPACE_HOST="$(prompt_default "OpenCode host code path (OPENCODE_WORKSPACE_HOST)" "$default_opencode")"
  fi
fi
validate_opencode_workspace "$OPENCODE_WORKSPACE_HOST"
mkdir -p "$OPENCODE_WORKSPACE_HOST"
upsert_env .env OPENCODE_WORKSPACE_HOST "$OPENCODE_WORKSPACE_HOST"

# --- data/ tree ---
# data/voice and data/projects are bind-mount sources for the hermes service.
# Create them here; if Docker gets there first it creates them owned by root,
# and Hermes can then write neither calibration output nor drafts.
mkdir -p "data/inputs/${WORKSPACE}" "data/rag_storage/${WORKSPACE}" "data/hermes" "data/voice" "data/projects"
if [[ ! -f "data/inputs/${WORKSPACE}/getting-started.md" ]]; then
  create_getting_started "$WORKSPACE"
fi
if [[ ! -f "data/voice/README.md" ]]; then
  create_voice_readme
fi
if [[ ! -f "data/projects/README.md" ]]; then
  create_projects_readme
fi
log "Created data/inputs/${WORKSPACE}/, data/rag_storage/${WORKSPACE}/, data/hermes/, data/voice/, and data/projects/"

# --- Default profile: core (Hermes + WebUI + SearXNG) ---
if ! grep -q '^COMPOSE_PROFILES=' .env 2>/dev/null; then
  upsert_env .env COMPOSE_PROFILES core
  log "Set COMPOSE_PROFILES=core"
elif [[ -z "$(grep '^COMPOSE_PROFILES=' .env | head -1 | cut -d= -f2-)" ]]; then
  upsert_env .env COMPOSE_PROFILES core
  log "Set COMPOSE_PROFILES=core"
else
  # Ensure core is present when profiles already exist
  append_compose_profile core
fi

# Hermes env files are required bind-mount sources for the core profile.
ensure_hermes_env_files
ensure_opencode_local_config

# LightRAG MCP stays off until rag is enabled (avoids connect timeouts on core-only).
set_env_if_missing .env LIGHTRAG_MCP_ENABLED 0

# --- Optional profiles ---
if [[ "$SETUP_RAG" -eq 1 ]]; then
  setup_rag_profile
fi
if [[ "$SETUP_AUTOMATION" -eq 1 ]]; then
  setup_automation_profile
fi
if [[ "$SETUP_CODING" -eq 1 ]]; then
  setup_coding_profile
fi
if [[ "$SETUP_OLLAMA" -eq 1 ]]; then
  setup_ollama_profile
fi

# --- n8n credentials (needed when automation is enabled later; cheap to always refresh) ---
set -a
# shellcheck disable=SC1091
source <(grep -E '^(N8N_ENCRYPTION_KEY|LIGHTRAG_API_KEY|NEO4J_PASSWORD|NEO4J_USERNAME)=' .env | sed 's/^/export /')
set +a
node scripts/generate-n8n-credentials.js
log "Regenerated n8n demo credentials"

PROFILES_NOW="$(grep '^COMPOSE_PROFILES=' .env | head -1 | cut -d= -f2- || echo core)"

cat <<EOF

Setup complete.

  Workspace:  ${WORKSPACE}
  Profiles:   ${PROFILES_NOW}
  Data:       data/inputs/${WORKSPACE}/
  OpenCode:   ${OPENCODE_WORKSPACE_HOST} (same path inside the container)

Next steps:
  1. Point .env at a model server (OpenAI-compatible host API / host Ollama), OR use --ollama.
  2. make doctor
  3. make up
  4. Open Hermes WebUI: http://localhost:8787
     Dashboard:         http://localhost:9119

Optional profiles (re-run setup with flags, or edit COMPOSE_PROFILES):
  --rag           Knowledge graph (LightRAG + Neo4j)
  --automation    n8n + GPT Researcher
  --coding        OpenCode
  --ollama        Bundled Ollama + demo model pull

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
First Hermes boot (once):
  docker compose run --rm hermes setup
  make up

EOF
