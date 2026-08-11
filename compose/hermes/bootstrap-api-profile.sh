#!/bin/sh
# Idempotent bootstrap for a Hermes gateway profile (max_turns, toolset, API port).
#
# Runs once per profile. The `api-server` profile is the unattended default; the
# `browser` profile (Hermes WebUI) reuses this script with a different name,
# port, turn cap, and toolset. Every profile-specific value is an env var, and
# the default-profile side effects below are idempotent, so running this a
# second time for a second profile is safe.
set -e

HERMES_HOME="${HERMES_HOME:-/opt/data}"
PROFILE="${HERMES_API_PROFILE_NAME:-api-server}"
MAX_TURNS="${HERMES_API_MAX_TURNS:-20}"
API_PORT="${HERMES_API_SERVER_PORT:-8643}"
# hermes-api-server is hermes-cli minus the interactive-only tools. Interactive
# front-ends (WebUI) want the full hermes-cli set back.
API_TOOLSET="${HERMES_API_TOOLSET:-hermes-api-server}"
SOURCE_ENV="${BOOTSTRAP_API_ENV:-/bootstrap/api-server.env}"
SKILLS_EXTERNAL_DIR="${HERMES_SKILLS_EXTERNAL_DIR:-/opt/skills}"
LIGHTRAG_MCP_URL="${LIGHTRAG_MCP_URL:-http://lightrag-mcp:8000/mcp}"
LIGHTRAG_MCP_TIMEOUT="${LIGHTRAG_MCP_TIMEOUT:-120}"
LIGHTRAG_MCP_CONNECT_TIMEOUT="${LIGHTRAG_MCP_CONNECT_TIMEOUT:-30}"
LIGHTRAG_MCP_ENABLED="${LIGHTRAG_MCP_ENABLED:-0}"
SEARXNG_MCP_URL="${SEARXNG_MCP_URL:-http://mcp-searxng:3000/mcp}"
SEARXNG_MCP_TIMEOUT="${SEARXNG_MCP_TIMEOUT:-60}"
SEARXNG_MCP_CONNECT_TIMEOUT="${SEARXNG_MCP_CONNECT_TIMEOUT:-30}"

PROFILE_DIR="${HERMES_HOME}/profiles/${PROFILE}"
PROFILE_ENV="${PROFILE_DIR}/.env"
DEFAULT_ENV="${HERMES_HOME}/.env"

log() {
  printf '[hermes-api-bootstrap] %s\n' "$1"
}

# Upsert KEY=VALUE in a dotenv file (replace existing line or append).
upsert_env() {
  file="$1"
  key="$2"
  value="$3"
  touch "$file"
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    tmp="$(mktemp)"
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        "${key}="*) printf '%s=%s\n' "$key" "$value" ;;
        *) printf '%s\n' "$line" ;;
      esac
    done < "$file" > "$tmp"
    mv "$tmp" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

# Remove KEY= lines from a dotenv file.
remove_env_key() {
  file="$1"
  key="$2"
  [ -f "$file" ] || return 0
  tmp="$(mktemp)"
  grep -v "^${key}=" "$file" > "$tmp" || true
  mv "$tmp" "$file"
}

# Path to a profile's config.yaml ("" selects the default profile).
config_path_for() {
  if [ -n "$1" ]; then
    printf '%s/profiles/%s/config.yaml\n' "$HERMES_HOME" "$1"
  else
    printf '%s/config.yaml\n' "$HERMES_HOME"
  fi
}

# Write a real YAML list at a dotted key path in a config.yaml.
#
# `hermes config set` takes a single scalar and stores it verbatim, which makes
# multi-element lists unwritable through the CLI: '["a","b"]' lands as a YAML
# *string*. Consumers then misread it instead of rejecting it. What happens is
# that _normalize_name_filter() turns the whole literal into a one-element
# allowlist no tool name can ever match, so an include filter written that way
# registers zero tools and logs nothing.
#
# Every key routed through here must be a list, so any scalar found at one is
# wrong by definition (bare path or stringified JSON array alike) and gets
# replaced. A real list is either already correct or a considered hand-edit, so
# those are left alone with a warning.
set_yaml_list() {
  _cfg="$1"
  _dotted="$2"
  shift 2
  _out="$(python3 - "$_cfg" "$_dotted" "$@" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, "/opt/hermes")
import yaml
from utils import atomic_yaml_write

cfg_path, dotted = sys.argv[1], sys.argv[2]
desired = sys.argv[3:]

# The same key is written to several profiles, so name the profile in the log.
parent = Path(cfg_path).parent
profile = parent.name if parent.parent.name == "profiles" else "default"
label = f"{dotted} ({profile})"

try:
    with open(cfg_path) as fh:
        config = yaml.safe_load(fh) or {}
except FileNotFoundError:
    print(f"ERROR: {cfg_path} not found")
    raise SystemExit(1)

parts = dotted.split(".")
node = config
for part in parts[:-1]:
    child = node.get(part)
    if not isinstance(child, dict):
        child = {}
        node[part] = child
    node = child
leaf = parts[-1]
current = node.get(leaf)

if current == desired:
    print(f"{label} already set")
elif current == [] or not isinstance(current, list):
    node[leaf] = list(desired)
    atomic_yaml_write(cfg_path, config)
    print(f"{'Setting' if current in (None, []) else 'Repairing'} {label}")
elif all(item in current for item in desired):
    # A longer list that still covers everything we need was added on purpose,
    # so leave it. Match on exact membership rather than substring, or
    # '/opt/skills-backup' would satisfy a requirement for '/opt/skills'.
    print(f"{label} already includes {', '.join(desired)}")
else:
    print(f"WARNING: {label} is {current!r} - not overwriting")
PY
)"
  if [ -n "$_out" ]; then log "$_out"; fi
}

# `hermes config set` for one profile ("" selects the default profile).
#
# sh has no local variables, so every helper here prefixes its own to avoid
# clobbering a caller's: this one runs inside register_mcp_server's loop.
hermes_config_set() {
  _hcs_profile="$1"
  _hcs_key="$2"
  _hcs_value="$3"
  if [ -n "$_hcs_profile" ]; then
    hermes -p "$_hcs_profile" config set "$_hcs_key" "$_hcs_value"
  else
    hermes config set "$_hcs_key" "$_hcs_value"
  fi
}

# Register an MCP server on one profile ("" selects the default profile).
#
# Individual keys are set rather than the whole mapping so MCP servers the user
# added by hand survive. Any trailing arguments become the tool allowlist, which
# must go through set_yaml_list: `hermes config set` stores its argument
# verbatim and cannot produce a YAML list (see the comment above it).
#
# Usage: register_mcp_server <profile> <key> <display> <url> <timeout> <connect_timeout> [tool...]
register_mcp_server() {
  _rms_profile="$1"
  _rms_key="$2"
  _rms_display="$3"
  _rms_url="$4"
  _rms_timeout="$5"
  _rms_connect_timeout="$6"
  shift 6
  log "Registering ${_rms_display} MCP server for profile '${_rms_profile:-default}' at ${_rms_url}"
  hermes_config_set "$_rms_profile" "mcp_servers.${_rms_key}.url" "$_rms_url"
  hermes_config_set "$_rms_profile" "mcp_servers.${_rms_key}.timeout" "$_rms_timeout"
  hermes_config_set "$_rms_profile" "mcp_servers.${_rms_key}.connect_timeout" "$_rms_connect_timeout"
  if [ "$#" -gt 0 ]; then
    set_yaml_list "$(config_path_for "$_rms_profile")" "mcp_servers.${_rms_key}.tools.include" "$@"
  fi
}

# Drop an unfiltered MCP server entry that duplicates one we register WITH a
# tool allowlist, on a managed profile only.
#
# Profiles are created with `profile create --clone`, so they inherit whatever
# MCP servers the default profile has, hand-added ones included. If the user
# registered the same server unfiltered under a different key (lightrag-mcp
# alongside our filtered lightrag), the clone carries both, and the unfiltered
# copy hands back every tool the allowlist exists to withhold. The allowlist
# only bounds anything while it is the ONLY route to that server.
#
# The match is narrow on purpose: same URL, and no tools.include of its own. A
# duplicate the user filtered themselves reads as a considered choice and stays.
# The default profile is never touched, since that is the user's own config.
#
# Usage: drop_unfiltered_mcp_duplicate <profile> <keep-key> <drop-key> <url>
drop_unfiltered_mcp_duplicate() {
  _dup_profile="$1"
  [ -n "$_dup_profile" ] || return 0
  _out="$(python3 - "$(config_path_for "$_dup_profile")" "$2" "$3" "$4" <<'PY'
import sys
sys.path.insert(0, "/opt/hermes")
import yaml
from utils import atomic_yaml_write

cfg_path, keep_key, drop_key, url = sys.argv[1:5]

try:
    with open(cfg_path) as fh:
        config = yaml.safe_load(fh) or {}
except FileNotFoundError:
    raise SystemExit(0)

servers = config.get("mcp_servers")
if not isinstance(servers, dict):
    raise SystemExit(0)

dup = servers.get(drop_key)
keep = servers.get(keep_key)
if not isinstance(dup, dict) or not isinstance(keep, dict):
    raise SystemExit(0)

if str(dup.get("url", "")).rstrip("/") != url.rstrip("/"):
    raise SystemExit(0)

if (dup.get("tools") or {}).get("include"):
    print(f"Keeping '{drop_key}' - it carries its own tool filter")
    raise SystemExit(0)

allowed = (keep.get("tools") or {}).get("include") or []
del servers[drop_key]
atomic_yaml_write(cfg_path, config)
print(
    f"Removed unfiltered duplicate MCP server '{drop_key}' ({url}) - "
    f"'{keep_key}' already exposes it, restricted to {len(allowed)} tools"
)
PY
)"
  if [ -n "$_out" ]; then log "$_out"; fi
}

# Web-reading MCP servers, registered on both the default and api-server
# profiles. Both are read-only against the public web, so unlike LightRAG they
# have no mutating tool to keep out of unattended sessions. The allowlists below
# just keep the tool count down; they draw no security boundary.
#
register_web_mcp_servers() {
  _rwms_profile="$1"
  register_mcp_server "$_rwms_profile" searxng SearXNG \
    "$SEARXNG_MCP_URL" "$SEARXNG_MCP_TIMEOUT" "$SEARXNG_MCP_CONNECT_TIMEOUT" \
    web_url_read searxng_web_search
}

if [ ! -f "${HERMES_HOME}/config.yaml" ]; then
  log "No ${HERMES_HOME}/config.yaml — run setup first:"
  log "  docker compose --profile hermes run --rm hermes setup"
  exit 0
fi

# --- Register repo-shipped skills directory (default profile) ---
# Runs before the api-server.env gate below so repo skills still load for users
# who never set up the API server profile.
set_yaml_list "$(config_path_for "")" "skills.external_dirs" "$SKILLS_EXTERNAL_DIR"

# --- Register web-reading MCP servers (default profile) ---
# Same rationale as the skills registration: dashboard and CLI sessions get URL
# reading whether or not the API server profile is ever configured.
register_web_mcp_servers ""

if [ ! -f "$SOURCE_ENV" ]; then
  env_name="$(basename "$SOURCE_ENV")"
  log "Missing ${SOURCE_ENV} — copy compose/hermes/${env_name}.example to compose/hermes/${env_name}"
  exit 1
fi

# --- Create profile if missing ---
profile_list="$(hermes profile list 2>/dev/null || true)"
case "$profile_list" in
  *"$PROFILE"*) log "Profile '${PROFILE}' already exists" ;;
  *)
    log "Creating profile '${PROFILE}' (clone from default)"
    hermes profile create "$PROFILE" --clone
    ;;
esac

mkdir -p "$PROFILE_DIR"

# Profiles carry their own config.yaml, so the default-profile registration
# above does not reach API sessions. Register here too, after creation.
set_yaml_list "$(config_path_for "$PROFILE")" "skills.external_dirs" "$SKILLS_EXTERNAL_DIR"

# --- Merge API env into profile .env ---
if [ ! -f "$PROFILE_ENV" ]; then
  touch "$PROFILE_ENV"
fi

# Apply API_SERVER_* from project file; skip comments and blank lines.
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ''|'#'*) continue ;;
    *=*)
      key="${line%%=*}"
      value="${line#*=}"
      # Strip inline comments (e.g. KEY=value # note).
      case "$value" in
        *' #'*) value="${value%% #*}" ;;
        *'#'*) value="${value%%#*}" ;;
      esac
      case "$key" in
        API_SERVER_*)
          if [ "$key" = "API_SERVER_PORT" ]; then
            upsert_env "$PROFILE_ENV" "$key" "$API_PORT"
          else
            upsert_env "$PROFILE_ENV" "$key" "$value"
          fi
          ;;
      esac
      ;;
  esac
done < "$SOURCE_ENV"

# Ensure port matches compose even if omitted from api-server.env.
upsert_env "$PROFILE_ENV" "API_SERVER_ENABLED" "true"
upsert_env "$PROFILE_ENV" "API_SERVER_HOST" "0.0.0.0"
upsert_env "$PROFILE_ENV" "API_SERVER_PORT" "$API_PORT"

remove_env_key "$PROFILE_ENV" "HERMES_MAX_ITERATIONS"

# --- Disable API server on default profile ---
touch "$DEFAULT_ENV"
upsert_env "$DEFAULT_ENV" "API_SERVER_ENABLED" "false"
remove_env_key "$DEFAULT_ENV" "HERMES_MAX_ITERATIONS"

# --- Apply profile config ---
# hermes-api-server matches dashboard chat (hermes-cli) minus the interactive-only
# tools (clarify, send_message, text_to_speech), which avoids web-only forced
# search loops. Interactive profiles pass HERMES_API_TOOLSET=hermes-cli to get
# those back; clarify turns an ambiguous browser request into a question instead
# of a guess, and there is a human present to answer it.
log "Setting agent.max_turns=${MAX_TURNS} and ${API_TOOLSET} toolset for api_server platform"
hermes -p "$PROFILE" config set "agent.max_turns" "$MAX_TURNS"
set_yaml_list "$(config_path_for "$PROFILE")" "platform_toolsets.api_server" "$API_TOOLSET"

# Register LightRAG MCP as read-oriented Knowledge Base access for API sessions.
# Opt-in via LIGHTRAG_MCP_ENABLED (set when the `rag` compose profile is on).
# Set individual keys so existing user-defined MCP servers are preserved.
#
# Treat the include list as a security boundary rather than a convenience. The
# server exposes 17 tools, 12 of which mutate the knowledge graph (insert_*,
# edit_*, and delete_by_doc_ids / delete_by_entities). Only the five
# read-oriented ones belong in unattended API sessions.
case "$LIGHTRAG_MCP_ENABLED" in
  1|true|TRUE|yes|YES)
    register_mcp_server "$PROFILE" lightrag LightRAG \
      "$LIGHTRAG_MCP_URL" "$LIGHTRAG_MCP_TIMEOUT" "$LIGHTRAG_MCP_CONNECT_TIMEOUT" \
      query_document get_documents get_pipeline_status get_graph_labels check_lightrag_health

    # The clone inherits the default profile's own unfiltered LightRAG entry,
    # which would hand this profile all 17 tools through the back door and leave
    # the allowlist above meaning nothing.
    drop_unfiltered_mcp_duplicate "$PROFILE" lightrag lightrag-mcp "$LIGHTRAG_MCP_URL"
    ;;
  *)
    log "Skipping LightRAG MCP registration (LIGHTRAG_MCP_ENABLED=${LIGHTRAG_MCP_ENABLED})"
    ;;
esac

# Same web-reading servers as the default profile above; profiles carry their
# own config.yaml, so the earlier registration does not reach API sessions.
register_web_mcp_servers "$PROFILE"

# --- Mark gateway for autostart (s6 reconciler in main hermes container) ---
# `hermes gateway start` is a no-op inside Docker ("Service start is not
# applicable inside a Docker container"). The s6 reconciler reads each
# profile's gateway_state.json and only auto-starts when gateway_state is
# "running" — seed that file directly (same contract as HERMES_GATEWAY_BOOTSTRAP_STATE).
GATEWAY_STATE_FILE="${PROFILE_DIR}/gateway_state.json"
log "Marking gateway for profile '${PROFILE}' as running (gateway_state.json)"
printf '{"gateway_state":"running"}\n' > "$GATEWAY_STATE_FILE"
chmod 644 "$GATEWAY_STATE_FILE" 2>/dev/null || true

log "Done: profile=${PROFILE} port=${API_PORT} max_turns=${MAX_TURNS} toolset=${API_TOOLSET}"
log "API URL (host): http://localhost:${API_PORT}/v1"
log "API URL (compose): http://hermes:${API_PORT}/v1"
