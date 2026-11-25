# The gitignored local overlay files compose mounts: compose/hermes/api-server.env
# and browser.env (with generated keys), searxng/settings.local.yml (with the
# SearXNG secret) and opencode/opencode.local.json (pointed at llm-proxy).
# setup.sh and up.sh (`make up`, `make ensure-local`) both create them through
# here. Needs ROOT. Safe under /bin/bash 3.2.
#
#   # shellcheck source=lib/local-files.sh
#   source "${ROOT}/scripts/lib/local-files.sh"
#   ensure_local_files

# shellcheck source=lib/env.sh
declare -F env_get_file > /dev/null || source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
# shellcheck source=lib/secret.sh
declare -F random_value > /dev/null || source "$(dirname "${BASH_SOURCE[0]}")/secret.sh"
# shellcheck source=lib/python.sh
declare -F stack_python > /dev/null || source "$(dirname "${BASH_SOURCE[0]}")/python.sh"

# Messages go through the caller's log and die when it has them (setup.sh
# prefixes [setup]), else plain lines.
local_files_log() {
  if declare -F log > /dev/null; then log "$1"; else printf '%s\n' "$1"; fi
}

local_files_die() {
  if declare -F die > /dev/null; then die "$1"; fi
  printf 'error: %s\n' "$1" >&2
  exit 1
}

# api-server.env and browser.env, each with a new 64-character API_SERVER_KEY
# when missing. An existing file keeps its key; browser.env gets
# HERMES_WEBUI_GATEWAY_API_KEY set to match it. Set LOCAL_FILES_QUIET=1 to
# skip the "Keeping existing" lines.
ensure_hermes_env_files() {
  local hermes_env="$ROOT/compose/hermes/api-server.env"
  local browser_env="$ROOT/compose/hermes/browser.env"
  if [[ ! -f "$hermes_env" ]]; then
    cp "$ROOT/compose/hermes/api-server.env.example" "$hermes_env"
    upsert_env "$hermes_env" API_SERVER_KEY "$(random_value hex 32)"
    local_files_log "Created compose/hermes/api-server.env"
  elif [[ "${LOCAL_FILES_QUIET:-0}" != 1 ]]; then
    local_files_log "Keeping existing compose/hermes/api-server.env"
  fi
  if [[ ! -f "$browser_env" ]]; then
    cp "$ROOT/compose/hermes/browser.env.example" "$browser_env"
    local browser_key
    browser_key="$(random_value hex 32)"
    upsert_env "$browser_env" API_SERVER_KEY "$browser_key"
    upsert_env "$browser_env" HERMES_WEBUI_GATEWAY_API_KEY "$browser_key"
    local_files_log "Created compose/hermes/browser.env"
  else
    # Keep an existing API_SERVER_KEY, but ensure the WebUI-preferred alias is
    # present and matches. WebUI's _reload_dotenv overwrites API_SERVER_KEY from
    # HERMES_HOME/.env; HERMES_WEBUI_GATEWAY_API_KEY survives that reload.
    local existing
    existing="$(env_get_file "$browser_env" API_SERVER_KEY)"
    existing="${existing%% #*}"
    existing="$(printf '%s' "$existing" | tr -d '\r')"
    if [[ -n "$existing" && "$(env_get_file "$browser_env" HERMES_WEBUI_GATEWAY_API_KEY)" != "$existing" ]]; then
      upsert_env "$browser_env" HERMES_WEBUI_GATEWAY_API_KEY "$existing"
    fi
    [[ "${LOCAL_FILES_QUIET:-0}" == 1 ]] || local_files_log "Keeping existing compose/hermes/browser.env"
  fi
}

# SEARXNG_SECRET from .env. A missing or change-me value is replaced with a
# new one, written back to .env when there is one.
searxng_secret() {
  local secret=""
  [[ -f "$ROOT/.env" ]] && secret="$(env_get_file "$ROOT/.env" SEARXNG_SECRET)"
  if [[ -z "$secret" || "$secret" == change-me* ]]; then
    secret="$(random_value hex 24)"
    if [[ -f "$ROOT/.env" ]]; then
      upsert_env "$ROOT/.env" SEARXNG_SECRET "$secret"
      local_files_log "Set SEARXNG_SECRET" >&2
    fi
  fi
  printf '%s' "$secret"
}

# sync_searxng_secret SECRET: write searxng/settings.local.yml from the
# tracked template with SECRET as its secret_key, replacing the file. Compose
# mounts the local file, which keeps setup from ever touching the tracked
# settings.yml.
sync_searxng_secret() {
  local secret="$1"
  local template="$ROOT/searxng/settings.yml"
  local settings="$ROOT/searxng/settings.local.yml"
  [[ -f "$template" ]] || local_files_die "Missing $template"
  local tmp line
  tmp="$(mktemp "$(dirname "$settings")/.$(basename "$settings").XXXXXX")"
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^[[:space:]]*secret_key:[[:space:]]* ]]; then
      printf '  secret_key: "%s"\n' "$secret"
    else
      printf '%s\n' "$line"
    fi
  done < "$template" > "$tmp"
  mv "$tmp" "$settings"
  local_files_log "Synced SEARXNG_SECRET into searxng/settings.local.yml"
}

# Write opencode/opencode.local.json (gitignored) and leave tracked
# opencode.json as a placeholder template (provider "local", model
# "your-chat-model"). Real model ids and baseURL belong in the local overlay
# only. Compose mounts that overlay when it exists. An existing file keeps its
# models; its permission block is updated from the template and its providers
# are pointed at llm-proxy.
ensure_opencode_local_config() {
  local base="$ROOT/opencode/opencode.json"
  local local_cfg="$ROOT/opencode/opencode.local.json"
  [[ -f "$base" ]] || local_files_die "Missing $base"
  if [[ ! -f "$local_cfg" ]]; then
    cp "$base" "$local_cfg"
    local_files_log "Created opencode/opencode.local.json from opencode.json (edit provider/model for your server)"
  else
    # Merge headless permission defaults from the template without clobbering
    # provider/model settings the operator already tuned.
    if stack_python - "$base" "$local_cfg" <<'PY'
import json, sys
from pathlib import Path
base = json.loads(Path(sys.argv[1]).read_text())
local_path = Path(sys.argv[2])
local = json.loads(local_path.read_text())
perm = base.get("permission")
changed = False
if perm and local.get("permission") != perm:
    local["permission"] = perm
    changed = True
if changed:
    local_path.write_text(json.dumps(local, indent=2) + "\n")
PY
    then
      local_files_log "Ensured permission block in opencode.local.json"
    fi
  fi
  stack_python - "$local_cfg" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
cfg = json.loads(path.read_text())
proxy = "http://llm-proxy:4000/v1"
providers = cfg.get("provider") or {}
for block in providers.values():
    if not isinstance(block, dict):
        continue
    options = block.get("options")
    if not isinstance(options, dict):
        options = {}
        block["options"] = options
    options["baseURL"] = proxy
    options["apiKey"] = "local-llm"
path.write_text(json.dumps(cfg, indent=2) + "\n")
PY
  local_files_log "Pointed OpenCode providers at llm-proxy"
}

# Create whichever local files are missing, the way setup.sh does, and leave
# existing ones as they are (apart from browser.env's matching alias). What
# up.sh runs before every start.
ensure_local_files() {
  LOCAL_FILES_QUIET=1 ensure_hermes_env_files
  [[ -f "$ROOT/searxng/settings.local.yml" ]] || sync_searxng_secret "$(searxng_secret)"
  [[ -f "$ROOT/opencode/opencode.local.json" ]] || ensure_opencode_local_config
}
