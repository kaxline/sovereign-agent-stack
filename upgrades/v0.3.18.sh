#!/usr/bin/env bash
# v0.3.18: Hermes Agent v0.23.2 / WebUI 0.52.702, image pin bumps, Twenty
# v2.55.0 -> v2.60.3. CHANGELOG.md "v0.3.18" has the detail.
#
# Backups this hook makes, each skipped when it already exists:
#   data/hermes.bak-<date>-pre-v0.23.2           Hermes home, hermes stopped
#   data/backups/n8n-before-2.48.7-<date>.tar    n8n_storage, n8n stopped
#   <TWENTY_DATA_DIR>/backups/twenty-before-v2.60.3-<date>.sql   pg_dump
# A backup that fails stops the upgrade before anything migrates.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ROOT="$UPGRADE_ROOT"
# shellcheck source=../scripts/lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"
cd "$ROOT"

TODAY="$(date +%F)"

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

# pin KEY OLD NEW: a .env line still holding v0.3.17's value moves to NEW. A
# value of the user's own stays, with a notice.
pin() {
  local cur
  cur="$(env_get "$1")"
  if [[ "$cur" == "$2" ]]; then
    upsert_env .env "$1" "$3"
    echo "${1}: ${2} -> ${3} in .env" >&2
  elif [[ -n "$cur" && "$cur" != "$3" ]]; then
    notice "${1}=${cur} in .env is kept; this release ships ${3}."
  fi
}

have_docker() { command -v docker > /dev/null 2>&1; }

project() {
  docker compose config 2> /dev/null | sed -n 's/^name: *//p' | head -1 || true
}

# service_ids SERVICE [running]: container ids of SERVICE in this project.
service_ids() {
  local args=(-q --filter "label=com.docker.compose.project=${PROJECT}"
    --filter "label=com.docker.compose.service=$1")
  if [[ "${2:-}" == running ]]; then args+=(--filter status=running); else args+=(-a); fi
  docker ps "${args[@]}" 2> /dev/null || true
}

# service_image SERVICE: the image SERVICE's container was made from, if any.
service_image() {
  local id
  id="$(service_ids "$1" | head -1)"
  [[ -n "$id" ]] || return 0
  docker inspect --format '{{.Config.Image}}' "$id" 2> /dev/null || true
}

stop_service() {
  local ids
  ids="$(service_ids "$1" running)"
  [[ -z "$ids" ]] && return 0
  # shellcheck disable=SC2086  # one id per word
  docker stop $ids > /dev/null || die "couldn't stop $1 for its backup"
  echo "stopped $1 for its backup" >&2
}

PROJECT=""
if have_docker; then PROJECT="$(project)"; fi

# --- Hermes Agent v0.23.2, WebUI 0.52.702 ---------------------------------------
# An unset tag meant v0.23.1 before this release and means v0.23.2 now.
agent="$(env_get HERMES_AGENT_IMAGE_TAG)"
if [[ -z "$agent" || "$agent" == v0.23.1 ]]; then
  if [[ -d data/hermes ]] && ! compgen -G "data/hermes.bak-*-pre-v0.23.2" > /dev/null; then
    if [[ -n "$PROJECT" ]]; then
      for s in hermes-webui hermes; do stop_service "$s"; done
    fi
    bak="data/hermes.bak-${TODAY}-pre-v0.23.2"
    rm -rf "${bak}.partial"
    if ! cp -Rp data/hermes "${bak}.partial"; then
      rm -rf "${bak}.partial"
      die "couldn't copy data/hermes to ${bak}. Make that copy yourself (sudo cp -Rp data/hermes ${bak} when some files belong to root), then run the upgrade again."
    fi
    mv "${bak}.partial" "$bak"
    echo "backed up data/hermes to ${bak}" >&2
  fi
fi
pin HERMES_AGENT_IMAGE_TAG v0.23.1 v0.23.2
pin HERMES_WEBUI_IMAGE_TAG 0.52.544 0.52.702

# The overlays changed (Signal env, send_message, digest redaction), so both
# bootstraps run again whatever the tag.
reload_hermes
notice "Hermes Agent is now v0.23.2 and WebUI 0.52.702. Run the verification block in docs/hermes-webui.md: the tool filters and skills.external_dirs are what silently regress."

# --- image pins ------------------------------------------------------------------
pin SIGNAL_CLI_IMAGE registry.gitlab.com/packaging/signal-cli/signal-cli-jre:v0-14-9-2 \
  registry.gitlab.com/packaging/signal-cli/signal-cli-jre:v0-14-10-1
pin MCP_SEARXNG_IMAGE isokoliuk/mcp-searxng:2.10.1 isokoliuk/mcp-searxng:2.11.0
pin VIKUNJA_PROXY_IMAGE nginx:1.30.6-alpine nginx:1.30.7-alpine
pin VAULTWARDEN_TLS_IMAGE nginx:1.30.6-alpine nginx:1.30.7-alpine
pin OLLAMA_IMAGE ollama/ollama:0.40.11 ollama/ollama:0.40.17
pin N8N_IMAGE n8nio/n8n:2.47.4 n8nio/n8n:2.48.7

# n8n migrates its database on first start, forward only. Back up its volume
# when the image is about to change.
if enabled AUTOMATION_ENABLED && [[ -n "$PROJECT" ]]; then
  n8n_new="$(env_get N8N_IMAGE n8nio/n8n:2.48.7)"
  n8n_vol="$(docker volume ls -q --filter "label=com.docker.compose.project=${PROJECT}" \
    --filter "label=com.docker.compose.volume=n8n_storage" 2> /dev/null | head -1 || true)"
  if [[ -n "$n8n_vol" && "$(service_image n8n)" != "$n8n_new" ]] \
    && ! compgen -G "data/backups/n8n-before-2.48.7-*.tar" > /dev/null; then
    stop_service n8n
    mkdir -p data/backups
    out="data/backups/n8n-before-2.48.7-${TODAY}.tar"
    # tar in place of n8n's entrypoint: n8n itself doesn't start, so nothing
    # migrates.
    if ! compose_with_profile automation run --rm --no-deps -T --entrypoint tar n8n \
      -C /home/node -cf - .n8n > "${out}.partial"; then
      rm -f "${out}.partial"
      die "couldn't back up n8n's data (volume ${n8n_vol}) to ${out}"
    fi
    mv "${out}.partial" "$out"
    echo "backed up n8n's data to ${out}" >&2
  fi
fi

# Rebuilt for their new versions: Bitwarden CLI 2026.11.0 in the broker's image
# (bitwarden-setup uses the same image), bitwarden-signin built FROM the agent
# image, OpenCode 1.19.6.
if enabled BITWARDEN_ENABLED; then
  rebuild bitwarden-broker
  rebuild bitwarden-signin
fi
if enabled OPENCODE_MCP_ENABLED; then
  rebuild opencode
fi

# --- Twenty v2.55.0 -> v2.60.3 -------------------------------------------------------
pin TWENTY_IMAGE twentycrm/twenty:v2.55.0 twentycrm/twenty:v2.60.3

if enabled TWENTY_LOCAL_ENABLED; then
  have_docker && [[ -n "$PROJECT" ]] \
    || die "the local Twenty instance is on, and backing up its database before v2.60.3 migrates it needs docker compose"
  tw_new="$(env_get TWENTY_IMAGE twentycrm/twenty:v2.60.3)"
  tw_dir="$(expand_user_path "$(env_get TWENTY_DATA_DIR ./data/twenty)")"
  if [[ "$(service_image twenty-server)" == "$tw_new" ]]; then
    echo "twenty-server already runs ${tw_new}; no dump" >&2
  elif compgen -G "${tw_dir}/backups/twenty-before-v2.60.3-*.sql" > /dev/null; then
    echo "a dump from before v2.60.3 is already in ${tw_dir}/backups" >&2
  else
    # Postgres's image doesn't change in this release; the hook runs before
    # the start, so twenty-server (which migrates) stays as it is.
    if [[ -z "$(service_ids twenty-db running)" ]]; then
      echo "starting twenty-db for the dump" >&2
      compose_with_profile crm up -d --wait twenty-db >&2 \
        || die "couldn't start twenty-db to dump it before v2.60.3"
    fi
    mkdir -p "${tw_dir}/backups"
    out="${tw_dir}/backups/twenty-before-v2.60.3-${TODAY}.sql"
    if ! (umask 077 && compose_with_profile crm exec -T twenty-db \
      pg_dump -U twenty twenty > "${out}.partial") || [[ ! -s "${out}.partial" ]]; then
      rm -f "${out}.partial"
      die "pg_dump of twenty-db failed; Twenty would migrate its database with no way back, so the upgrade stops here"
    fi
    mv "${out}.partial" "$out"
    echo "dumped Twenty's database to ${out}" >&2
    notice "Twenty's database from before v2.60.3 is in ${out}. A restore also needs TWENTY_ENCRYPTION_KEY and TWENTY_DB_PASSWORD from .env; keep them with it."
  fi

  # A stored local workspace gets the read-only role's per-object permissions
  # only from crm init, which needs the migrated server up.
  store="$(resolve_data_root_path)/addons/twenty/instances"
  if grep -Elq '"local": *true' "$store"/*.json 2> /dev/null; then
    needs_user twenty-crm-init \
      "Twenty is now v2.60.3. Once it is up, run make crm-init ROTATE=1, then make hermes-reload: it re-applies the read-only role's permissions on every object (without it the read-only key can still change agentChatThreadTarget rows) and mints new keys."
  fi
fi
