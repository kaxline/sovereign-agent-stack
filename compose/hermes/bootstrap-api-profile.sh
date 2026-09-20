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
# Interactive sessions (dashboard / CLI / WebUI) review memory every 3 user
# turns. The factory default of 10 almost never fires: most chats here are
# 1–2 turns. Unattended API sessions keep a slower cadence so one-shot n8n
# jobs do not distill themselves into USER.md. The clone inherits the
# default-profile value, so the API profile must be set back explicitly.
INTERACTIVE_MEMORY_NUDGE="${HERMES_INTERACTIVE_MEMORY_NUDGE_INTERVAL:-3}"
PROFILE_MEMORY_NUDGE="${HERMES_MEMORY_NUDGE_INTERVAL:-10}"
LIGHTRAG_MCP_URL="${LIGHTRAG_MCP_URL:-http://lightrag-mcp:8000/mcp}"
LIGHTRAG_MCP_TIMEOUT="${LIGHTRAG_MCP_TIMEOUT:-120}"
LIGHTRAG_MCP_CONNECT_TIMEOUT="${LIGHTRAG_MCP_CONNECT_TIMEOUT:-30}"
LIGHTRAG_MCP_ENABLED="${LIGHTRAG_MCP_ENABLED:-0}"
SEARXNG_MCP_URL="${SEARXNG_MCP_URL:-http://mcp-searxng:3000/mcp}"
SEARXNG_MCP_TIMEOUT="${SEARXNG_MCP_TIMEOUT:-60}"
SEARXNG_MCP_CONNECT_TIMEOUT="${SEARXNG_MCP_CONNECT_TIMEOUT:-30}"
CALDAV_MCP_URL="${CALDAV_MCP_URL:-http://caldav-mcp:8080/mcp}"
CALDAV_MCP_TIMEOUT="${CALDAV_MCP_TIMEOUT:-60}"
CALDAV_MCP_CONNECT_TIMEOUT="${CALDAV_MCP_CONNECT_TIMEOUT:-30}"
CALDAV_MCP_ENABLED="${CALDAV_MCP_ENABLED:-0}"
CALDAV_MCP_ACCOUNTS_DIR="${CALDAV_MCP_ACCOUNTS_DIR:-/bootstrap/caldav-accounts}"

PROFILE_DIR="${HERMES_HOME}/profiles/${PROFILE}"
PROFILE_ENV="${PROFILE_DIR}/.env"
DEFAULT_ENV="${HERMES_HOME}/.env"

log() {
  printf '[hermes-api-bootstrap] %s\n' "$1"
}

# Replace a dotenv file from a temp path. Use cat+rm instead of mv: Docker Desktop
# bind mounts reject `mv tmp existing-file` with "File exists" when two bootstrap
# services write the same path concurrently.
replace_file() {
  _tmp="$1"
  _file="$2"
  cat "$_tmp" > "$_file" && rm -f "$_tmp"
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
    replace_file "$tmp" "$file"
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
  replace_file "$tmp" "$file"
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

# Ensure each desired item appears in a YAML list, preserving any extra user
# entries. Unlike set_yaml_list, a shorter current list is extended rather than
# left alone with a warning — needed when we add new disabled toolsets over
# time (e.g. todo -> todo+web) without wiping hand-edited extras.
ensure_yaml_list_items() {
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

if not isinstance(current, list):
    node[leaf] = list(desired)
    atomic_yaml_write(cfg_path, config)
    print(f"Setting {label}")
elif all(item in current for item in desired):
    print(f"{label} already includes {', '.join(desired)}")
else:
    merged = list(current)
    added = []
    for item in desired:
        if item not in merged:
            merged.append(item)
            added.append(item)
    node[leaf] = merged
    atomic_yaml_write(cfg_path, config)
    print(f"Adding {', '.join(added)} to {label}")
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

# Register one CalDAV account as mcp_servers.caldav-<slug> with X-Caldav-* headers.
# hermes config set cannot write nested header maps, so this uses atomic_yaml_write.
#
# Usage: register_caldav_account <profile> <slug> <url> <username> <password> <timeout> <connect_timeout> [tool...]
register_caldav_account() {
  _rca_profile="$1"
  _rca_slug="$2"
  _rca_url="$3"
  _rca_user="$4"
  _rca_pass="$5"
  _rca_timeout="$6"
  _rca_connect="$7"
  shift 7
  _rca_key="caldav-${_rca_slug}"
  _rca_cfg="$(config_path_for "$_rca_profile")"
  log "Registering CalDAV MCP server '${_rca_key}' for profile '${_rca_profile:-default}'"
  _out="$(python3 - "$_rca_cfg" "$_rca_key" "$CALDAV_MCP_URL" "$_rca_url" "$_rca_user" "$_rca_pass" \
    "$_rca_timeout" "$_rca_connect" "$@" <<'PY'
import sys
sys.path.insert(0, "/opt/hermes")
import yaml
from utils import atomic_yaml_write

cfg_path, key, mcp_url, caldav_url, username, password, timeout, connect_timeout = sys.argv[1:9]
tools = sys.argv[9:]

try:
    with open(cfg_path) as fh:
        config = yaml.safe_load(fh) or {}
except FileNotFoundError:
    print(f"ERROR: {cfg_path} not found")
    raise SystemExit(1)

servers = config.setdefault("mcp_servers", {})
if not isinstance(servers, dict):
    servers = {}
    config["mcp_servers"] = servers

entry = servers.get(key)
if not isinstance(entry, dict):
    entry = {}
entry["url"] = mcp_url
entry["timeout"] = int(timeout) if str(timeout).isdigit() else timeout
entry["connect_timeout"] = int(connect_timeout) if str(connect_timeout).isdigit() else connect_timeout
entry["headers"] = {
    "X-Caldav-Url": caldav_url,
    "X-Caldav-Username": username,
    "X-Caldav-Password": password,
}
if tools:
    tools_block = entry.get("tools")
    if not isinstance(tools_block, dict):
        tools_block = {}
    tools_block["include"] = list(tools)
    entry["tools"] = tools_block
servers[key] = entry
atomic_yaml_write(cfg_path, config)
print(f"Wrote mcp_servers.{key} ({len(tools)} tools)" if tools else f"Wrote mcp_servers.{key}")
PY
)"
  if [ -n "$_out" ]; then log "$_out"; fi
}

# Drop mcp_servers.caldav-* keys that no longer have a matching accounts/<slug>.env.
# Usage: reconcile_caldav_accounts <profile> <slug...>
reconcile_caldav_accounts() {
  _rec_profile="$1"
  shift
  _rec_cfg="$(config_path_for "$_rec_profile")"
  _out="$(python3 - "$_rec_cfg" "$@" <<'PY'
import sys
sys.path.insert(0, "/opt/hermes")
import yaml
from utils import atomic_yaml_write

cfg_path = sys.argv[1]
keep_slugs = set(sys.argv[2:])
keep_keys = {f"caldav-{s}" for s in keep_slugs}

try:
    with open(cfg_path) as fh:
        config = yaml.safe_load(fh) or {}
except FileNotFoundError:
    raise SystemExit(0)

servers = config.get("mcp_servers")
if not isinstance(servers, dict):
    raise SystemExit(0)

removed = []
for key in list(servers):
    if not key.startswith("caldav-"):
        continue
    if key in keep_keys:
        continue
    del servers[key]
    removed.append(key)

if removed:
    atomic_yaml_write(cfg_path, config)
    print("Removed stale CalDAV MCP entries: " + ", ".join(sorted(removed)))
PY
)"
  if [ -n "$_out" ]; then log "$_out"; fi
}

# Discover compose/caldav-mcp/accounts/*.env and register each as caldav-<slug>.
# api-server gets the read-only allowlist; default and browser get all tools.
register_caldav_mcp_servers() {
  _rcms_profile="$1"
  case "$CALDAV_MCP_ENABLED" in
    1|true|TRUE|yes|YES) ;;
    *)
      log "Skipping CalDAV MCP registration (CALDAV_MCP_ENABLED=${CALDAV_MCP_ENABLED})"
      return 0
      ;;
  esac

  if [ ! -d "$CALDAV_MCP_ACCOUNTS_DIR" ]; then
    log "WARNING: CalDAV accounts dir missing (${CALDAV_MCP_ACCOUNTS_DIR}) - skip registration"
    return 0
  fi

  # Read-only tools for unattended api-server; full surface elsewhere.
  if [ "$_rcms_profile" = "api-server" ]; then
    set -- \
      caldav_list_calendars \
      caldav_get_events \
      caldav_get_today_events \
      caldav_get_week_events \
      caldav_get_event_by_uid \
      caldav_search_events \
      caldav_get_freebusy \
      caldav_list_attendees
  else
    set -- \
      caldav_list_calendars \
      caldav_get_events \
      caldav_get_today_events \
      caldav_get_week_events \
      caldav_get_event_by_uid \
      caldav_search_events \
      caldav_get_freebusy \
      caldav_list_attendees \
      caldav_create_event \
      caldav_update_event \
      caldav_delete_event \
      caldav_move_event \
      caldav_add_attendee \
      caldav_remove_attendee
  fi

  _rcms_slugs=""
  _rcms_count=0
  for _rcms_file in "$CALDAV_MCP_ACCOUNTS_DIR"/*.env; do
    [ -f "$_rcms_file" ] || continue
    _rcms_base="$(basename "$_rcms_file")"
    case "$_rcms_base" in
      *.example|.*) continue ;;
    esac
    _rcms_slug="${_rcms_base%.env}"
    case "$_rcms_slug" in
      *[!a-z0-9-]*|'')
        log "WARNING: Skipping CalDAV account '${_rcms_base}' - slug must match [a-z0-9-]+"
        continue
        ;;
    esac

    _rcms_url=""
    _rcms_user=""
    _rcms_pass=""
    while IFS= read -r _rcms_line || [ -n "$_rcms_line" ]; do
      case "$_rcms_line" in
        ''|'#'*) continue ;;
        *=*)
          _rcms_k="${_rcms_line%%=*}"
          _rcms_v="${_rcms_line#*=}"
          case "$_rcms_v" in
            *' #'*) _rcms_v="${_rcms_v%% #*}" ;;
          esac
          # Trim CR and surrounding quotes.
          _rcms_v="$(printf '%s' "$_rcms_v" | tr -d '\r')"
          case "$_rcms_v" in
            \"*\") _rcms_v="${_rcms_v#\"}"; _rcms_v="${_rcms_v%\"}" ;;
            \'*\') _rcms_v="${_rcms_v#\'}"; _rcms_v="${_rcms_v%\'}" ;;
          esac
          case "$_rcms_k" in
            CALDAV_URL) _rcms_url="$_rcms_v" ;;
            CALDAV_USERNAME) _rcms_user="$_rcms_v" ;;
            CALDAV_PASSWORD) _rcms_pass="$_rcms_v" ;;
          esac
          ;;
      esac
    done < "$_rcms_file"

    if [ -z "$_rcms_url" ] || [ -z "$_rcms_user" ] || [ -z "$_rcms_pass" ]; then
      log "WARNING: Skipping CalDAV account '${_rcms_slug}' - missing CALDAV_URL/USERNAME/PASSWORD"
      continue
    fi
    case "$_rcms_pass" in
      xxxx-xxxx-xxxx-xxxx|change-me*|changeme*|your-*|placeholder*)
        log "WARNING: Skipping CalDAV account '${_rcms_slug}' - password still looks like a placeholder"
        continue
        ;;
    esac
    case "$_rcms_user" in
      you@icloud.com|your-*|change-me*|user@example.com)
        log "WARNING: Skipping CalDAV account '${_rcms_slug}' - username still looks like a placeholder"
        continue
        ;;
    esac

    register_caldav_account "$_rcms_profile" "$_rcms_slug" \
      "$_rcms_url" "$_rcms_user" "$_rcms_pass" \
      "$CALDAV_MCP_TIMEOUT" "$CALDAV_MCP_CONNECT_TIMEOUT" "$@"
    _rcms_slugs="${_rcms_slugs} ${_rcms_slug}"
    _rcms_count=$((_rcms_count + 1))
  done

  # shellcheck disable=SC2086
  reconcile_caldav_accounts "$_rcms_profile" $_rcms_slugs

  if [ "$_rcms_count" -eq 0 ]; then
    log "WARNING: CALDAV_MCP_ENABLED=1 but no valid accounts in ${CALDAV_MCP_ACCOUNTS_DIR}"
    log "  Copy compose/caldav-mcp/account.env.example to accounts/<slug>.env and fill credentials"
  else
    log "Registered ${_rcms_count} CalDAV account(s) on profile '${_rcms_profile:-default}'"
  fi
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

log "Setting memory.nudge_interval=${INTERACTIVE_MEMORY_NUDGE} on default profile"
hermes_config_set "" "memory.nudge_interval" "$INTERACTIVE_MEMORY_NUDGE"

# --- Register web-reading MCP servers (default profile) ---
# Same rationale as the skills registration: dashboard and CLI sessions get URL
# reading whether or not the API server profile is ever configured.
register_web_mcp_servers ""
register_caldav_mcp_servers ""

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
# Drop any leftover default-profile API_SERVER_KEY. Hermes WebUI loads
# HERMES_HOME/.env via _reload_dotenv and overwrites container env; a stale
# key here replaces the browser.env credential and every WebUI turn 401s.
remove_env_key "$DEFAULT_ENV" "API_SERVER_KEY"
remove_env_key "$DEFAULT_ENV" "API_SERVER_HOST"
remove_env_key "$DEFAULT_ENV" "API_SERVER_PORT"
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

# The built-in `todo` toolset is in-memory per chat session. WebUI users treat
# "add to the todo list" as durable; the model then reports an empty list in the
# next thread. Disable it on every profile this bootstrap touches so living
# todos go through the living-todos skill + /opt/projects/project-todo-list.
#
# Also disable the native `web` toolset (web_search / web_extract). With
# SEARXNG_URL set, Hermes auto-binds both to SearXNG: search works but extract
# always fails ("search-only backend"), and models ignore soft routing and spray
# parallel native web_search calls that empty upstream engines. MCP searxng
# (searxng_web_search + web_url_read) is the supported path on this stack.
log "Disabling session-only todo and native web toolsets on default and '${PROFILE}' profiles"
ensure_yaml_list_items "$(config_path_for "")" "agent.disabled_toolsets" "todo" "web"
ensure_yaml_list_items "$(config_path_for "$PROFILE")" "agent.disabled_toolsets" "todo" "web"

log "Setting memory.nudge_interval=${PROFILE_MEMORY_NUDGE} on profile '${PROFILE}'"
hermes_config_set "$PROFILE" "memory.nudge_interval" "$PROFILE_MEMORY_NUDGE"

# Interactive WebUI sessions hit a local LM Studio with one slot. Qwen-family
# models often answer "Let me search/read/check …" with finish_reason=stop and
# zero tool calls; Hermes' intent-ack continuation nudges those turns to
# continue, but only when agent.intent_ack_continuation is enabled (default
# "auto" limits it to codex_responses). Turn it on for hermes-cli profiles.
if [ "$API_TOOLSET" = "hermes-cli" ]; then
  log "Enabling agent.intent_ack_continuation=true on profile '${PROFILE}'"
  hermes_config_set "$PROFILE" "agent.intent_ack_continuation" "true"
fi

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
register_caldav_mcp_servers "$PROFILE"

# --- Mark gateway for autostart (s6 reconciler in main hermes container) ---
# `hermes gateway start` is a no-op inside Docker ("Service start is not
# applicable inside a Docker container"). The s6 reconciler reads each
# profile's gateway_state.json and only auto-starts when gateway_state is
# "running" — seed that file directly (same contract as HERMES_GATEWAY_BOOTSTRAP_STATE).
GATEWAY_STATE_FILE="${PROFILE_DIR}/gateway_state.json"
log "Marking gateway for profile '${PROFILE}' as running (gateway_state.json)"
printf '{"gateway_state":"running"}\n' > "$GATEWAY_STATE_FILE"
chmod 644 "$GATEWAY_STATE_FILE" 2>/dev/null || true

# Browser profile never owns the Signal SSE lock — default gateway does.
# Pin adapter disabled at bootstrap so fresh installs work before a second
# sync-signal-profile run (config.yaml is created here, not at ensure-local).
if [ "$PROFILE" = "browser" ]; then
  browser_cfg="$(config_path_for "$PROFILE")"
  if [ -f "$browser_cfg" ]; then
    log "Pinning platforms.signal.enabled=false on browser profile (default owns SSE)"
    python3 - "$browser_cfg" <<'PY'
import sys
from pathlib import Path

import yaml

path = Path(sys.argv[1])
data = yaml.safe_load(path.read_text()) or {}
platforms = data.setdefault("platforms", {})
signal = platforms.setdefault("signal", {})
if signal.get("enabled") is not False:
    signal["enabled"] = False
    platforms["signal"] = signal
    data["platforms"] = platforms
    path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    print("pinned platforms.signal.enabled=false")
else:
    print("platforms.signal.enabled already false")
PY
  fi
fi

log "Done: profile=${PROFILE} port=${API_PORT} max_turns=${MAX_TURNS} toolset=${API_TOOLSET}"
log "API URL (host): http://localhost:${API_PORT}/v1"
log "API URL (compose): http://hermes:${API_PORT}/v1"
