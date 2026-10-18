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
OPENCODE_MCP_URL="${OPENCODE_MCP_URL:-http://opencode-mcp:8000/sse}"
OPENCODE_MCP_TIMEOUT="${OPENCODE_MCP_TIMEOUT:-600}"
OPENCODE_MCP_CONNECT_TIMEOUT="${OPENCODE_MCP_CONNECT_TIMEOUT:-30}"
OPENCODE_MCP_ENABLED="${OPENCODE_MCP_ENABLED:-0}"
SQL_MCP_ENABLED="${SQL_MCP_ENABLED:-0}"
SQL_MCP_RO_URL="${SQL_MCP_RO_URL:-http://sql-mcp-ro:8000/sse}"
SQL_MCP_RW_URL="${SQL_MCP_RW_URL:-http://sql-mcp-rw:8000/sse}"
SQL_MCP_TIMEOUT="${SQL_MCP_TIMEOUT:-60}"
SQL_MCP_CONNECT_TIMEOUT="${SQL_MCP_CONNECT_TIMEOUT:-30}"
# postgres-mcp also ships index/health/top-query analysers; the agent needs
# none of them to read or keep data.
SQL_MCP_TOOLS="list_schemas list_objects get_object_details execute_sql explain_query"
VIKUNJA_MCP_ENABLED="${VIKUNJA_MCP_ENABLED:-0}"
VIKUNJA_MCP_URL="${VIKUNJA_MCP_URL:-http://vikunja:3456/api/v2/mcp}"
VIKUNJA_MCP_TIMEOUT="${VIKUNJA_MCP_TIMEOUT:-60}"
VIKUNJA_MCP_CONNECT_TIMEOUT="${VIKUNJA_MCP_CONNECT_TIMEOUT:-30}"
VIKUNJA_MCP_TOKEN_RO="${VIKUNJA_MCP_TOKEN_RO:-}"
VIKUNJA_MCP_TOKEN_RW="${VIKUNJA_MCP_TOKEN_RW:-}"
# Vikunja serves ~26 tools and filters them by the token's scopes. Pin only the
# everyday ones; the rest stay reachable through tool search.
# Hermes server key. Users talk about "todos", so the agent's tools are named
# mcp__todos__* rather than after the engine. Not "todo": Hermes aliases each MCP
# server name to its toolset, and "todo" is the built-in toolset disabled below.
VIKUNJA_MCP_KEY="todos"
VIKUNJA_MCP_PIN_RO="tasks_list tasks_read projects_list"
VIKUNJA_MCP_PIN_RW="tasks_list tasks_read tasks_create tasks_update projects_list"
MAIL_MCP_ENABLED="${MAIL_MCP_ENABLED:-0}"
MAIL_MCP_URL="${MAIL_MCP_URL:-http://mail-mcp:8000/mcp}"
MAIL_MCP_TIMEOUT="${MAIL_MCP_TIMEOUT:-60}"
MAIL_MCP_CONNECT_TIMEOUT="${MAIL_MCP_CONNECT_TIMEOUT:-30}"
MAIL_MCP_TOOLS="mail_status mail_search mail_get mail_thread"
MAIL_DRAFTS_ENABLED="${MAIL_DRAFTS_ENABLED:-0}"
MAIL_DRAFTS_MCP_URL="${MAIL_DRAFTS_MCP_URL:-http://mail-drafts-mcp:8000/mcp}"
MAIL_DRAFTS_MCP_TIMEOUT="${MAIL_DRAFTS_MCP_TIMEOUT:-60}"
MAIL_DRAFTS_MCP_CONNECT_TIMEOUT="${MAIL_DRAFTS_MCP_CONNECT_TIMEOUT:-30}"
MAIL_DRAFTS_MCP_TOOLS="mail_draft_create mail_draft_status"
# llm-proxy's real upstream. Only used to tell a local model server from a
# cloud API; the key never reaches this container.
LLM_BINDING_HOST="${LLM_BINDING_HOST:-http://host.docker.internal:1234/v1}"
# Empty = 300s for a local upstream, 180s for a cloud one.
PROXY_STALE_TIMEOUT="${HERMES_PROXY_STALE_TIMEOUT:-}"
# model.max_tokens for every profile on the proxy that has none. 0 = leave unset.
MODEL_MAX_TOKENS="${HERMES_MAX_TOKENS:-16384}"
# auto = off for a local upstream, on for a cloud one. 0/1 force it.
BROWSER_AUTO_TITLE="${HERMES_BROWSER_AUTO_TITLE:-auto}"

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
try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import sys as _sys
    _sys.path.insert(0, "/opt/hermes")
    import hermes_yaml as yaml
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
try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import sys as _sys
    _sys.path.insert(0, "/opt/hermes")
    import hermes_yaml as yaml
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

# Missing or non-list (scalar from `hermes config set`, stringified JSON)
# must merge into desired, not replace the whole key. Replacing is how
# a late OpenCode pin can leave only coding_* in always_include.
if isinstance(current, str) and current.strip():
    current = [current]
elif not isinstance(current, list):
    current = []
if all(item in current for item in desired):
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
    print(f"{'Setting' if not current else 'Adding ' + ', '.join(added) + ' to'} {label}")
PY
)"
  if [ -n "$_out" ]; then log "$_out"; fi
}

# True when a URL points at a model server on this machine or LAN. Mirrors
# Hermes' is_local_endpoint, plus host.docker.internal.
upstream_is_local() {
  python3 - "$1" <<'PY'
import ipaddress
import sys
from urllib.parse import urlsplit

host = (urlsplit(sys.argv[1]).hostname or "").lower()
local = (
    not host
    or host == "localhost"
    or "." not in host
    or host.endswith((".local", ".internal", ".lan", ".home.arpa"))
)
if not local:
    try:
        addr = ipaddress.ip_address(host)
        local = (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or (addr.version == 4 and addr in ipaddress.ip_network("100.64.0.0/10"))
        )
    except ValueError:
        pass
sys.exit(0 if local else 1)
PY
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

# True when a profile's config.yaml has a non-null value at a dotted key.
config_has_value() {
  python3 - "$(config_path_for "$1")" "$2" <<'PY'
import sys

try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import sys as _sys
    _sys.path.insert(0, "/opt/hermes")
    import hermes_yaml as yaml

try:
    with open(sys.argv[1]) as fh:
        node = yaml.safe_load(fh) or {}
except FileNotFoundError:
    raise SystemExit(1)
for part in sys.argv[2].split("."):
    node = node.get(part) if isinstance(node, dict) else None
raise SystemExit(0 if node is not None else 1)
PY
}

# bot_desktop.placement=auto can start the browser on the SSH backend, which
# has no Node or browser. gateway keeps it in this container. If the CLI
# rejects the key, write the YAML and continue.
set_bot_desktop_placement() {
  _sbdp_profile="$1"
  _sbdp_value="$2"
  if hermes_config_set "$_sbdp_profile" bot_desktop.placement "$_sbdp_value"; then
    return 0
  fi
  log "hermes config set rejected bot_desktop.placement; writing YAML for '${_sbdp_profile:-default}'"
  _sbdp_cfg="$(config_path_for "$_sbdp_profile")"
  if [ ! -f "$_sbdp_cfg" ]; then
    log "No config at ${_sbdp_cfg}; bot_desktop.placement was not written"
    return 0
  fi
  python3 - "$_sbdp_cfg" "$_sbdp_value" <<'PY'
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import sys as _sys
    _sys.path.insert(0, "/opt/hermes")
    import hermes_yaml as yaml
from utils import atomic_yaml_write

path = Path(sys.argv[1])
value = sys.argv[2]
data = yaml.safe_load(path.read_text()) or {}
bot = data.get("bot_desktop")
if not isinstance(bot, dict):
    bot = {}
if bot.get("placement") == value:
    print(f"bot_desktop.placement already {value}")
else:
    bot["placement"] = value
    data["bot_desktop"] = bot
    atomic_yaml_write(path, data)
    print(f"set bot_desktop.placement={value}")
PY
}

# Point shell and file tools at hermes-worker. The private key mount is
# read-only; cont-init copies it to this path with mode 600. Unset
# env_passthrough so the agent environment cannot ride along with ssh.
configure_terminal_ssh() {
  _cts_profile="$1"
  log "Terminal backend ssh for profile '${_cts_profile:-default}'"
  hermes_config_set "$_cts_profile" terminal.backend ssh
  hermes_config_set "$_cts_profile" terminal.ssh_host hermes-worker
  hermes_config_set "$_cts_profile" terminal.ssh_user hermes
  hermes_config_set "$_cts_profile" terminal.ssh_port 22
  hermes_config_set "$_cts_profile" terminal.ssh_key /var/lib/hermes-worker-key/id_ed25519
  hermes_config_set "$_cts_profile" terminal.cwd /opt/projects
  set_bot_desktop_placement "$_cts_profile" gateway
  _cts_cfg="$(config_path_for "$_cts_profile")"
  if [ -f "$_cts_cfg" ]; then
    python3 - "$_cts_cfg" <<'PY'
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import sys as _sys
    _sys.path.insert(0, "/opt/hermes")
    import hermes_yaml as yaml
from utils import atomic_yaml_write

path = Path(sys.argv[1])
data = yaml.safe_load(path.read_text()) or {}
terminal = data.get("terminal")
if isinstance(terminal, dict) and "env_passthrough" in terminal:
    terminal.pop("env_passthrough", None)
    data["terminal"] = terminal
    atomic_yaml_write(path, data)
    print("unset terminal.env_passthrough")
PY
  fi
}

# Escape hatch. Drop leftover ssh keys so a previous ssh profile cannot
# keep sending the shell to a worker this mode does not start.
configure_terminal_local() {
  _ctl_profile="$1"
  log "Terminal backend local for profile '${_ctl_profile:-default}'"
  hermes_config_set "$_ctl_profile" terminal.backend local
  set_bot_desktop_placement "$_ctl_profile" auto
  _ctl_cfg="$(config_path_for "$_ctl_profile")"
  if [ -f "$_ctl_cfg" ]; then
    python3 - "$_ctl_cfg" <<'PY'
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import sys as _sys
    _sys.path.insert(0, "/opt/hermes")
    import hermes_yaml as yaml
from utils import atomic_yaml_write

path = Path(sys.argv[1])
data = yaml.safe_load(path.read_text()) or {}
terminal = data.get("terminal")
if not isinstance(terminal, dict):
    raise SystemExit(0)
removed = []
for key in ("ssh_host", "ssh_user", "ssh_port", "ssh_key", "env_passthrough"):
    if key in terminal:
        terminal.pop(key, None)
        removed.append(key)
if removed:
    data["terminal"] = terminal
    atomic_yaml_write(path, data)
    print("unset " + ", ".join("terminal." + key for key in removed))
PY
  fi
}

apply_terminal_backend_all_profiles() {
  _atb_backend="${HERMES_TERMINAL_BACKEND:-ssh}"
  case "$_atb_backend" in
    ssh) _atb_fn=configure_terminal_ssh ;;
    local) _atb_fn=configure_terminal_local ;;
    *)
      log "HERMES_TERMINAL_BACKEND must be ssh or local (got ${_atb_backend})"
      exit 1
      ;;
  esac
  "$_atb_fn" ""
  if [ -n "$PROFILE" ]; then
    "$_atb_fn" "$PROFILE"
  fi
  if [ -d "${HERMES_HOME}/profiles" ]; then
    for _atb_dir in "${HERMES_HOME}/profiles"/*; do
      [ -d "$_atb_dir" ] || continue
      _atb_name="$(basename "$_atb_dir")"
      [ "$_atb_name" = "$PROFILE" ] && continue
      "$_atb_fn" "$_atb_name"
    done
  fi
}

# Model calls go to llm-proxy. The placeholder is not a secret. The real
# key stays on the proxy. Same profile sweep as the SSH backend. Every
# profile on the proxy also gets the stale-stream timeout sized below, and an
# output cap unless it already has one (browser keeps its 1024).
configure_model_proxy() {
  _cmp_profile="$1"
  log "Model proxy for profile '${_cmp_profile:-default}'"
  hermes_config_set "$_cmp_profile" model.base_url http://llm-proxy:4000/v1
  hermes_config_set "$_cmp_profile" model.api_key local-llm
  hermes_config_set "$_cmp_profile" agent.local_stream_stale_timeout "$STALE_TIMEOUT"
  if [ "$MODEL_MAX_TOKENS" != "0" ]; then
    if config_has_value "$_cmp_profile" model.max_tokens; then
      log "model.max_tokens already set on '${_cmp_profile:-default}'; keeping it"
    else
      hermes_config_set "$_cmp_profile" model.max_tokens "$MODEL_MAX_TOKENS"
    fi
  fi
}

apply_model_proxy_all_profiles() {
  configure_model_proxy ""
  if [ -n "$PROFILE" ]; then
    configure_model_proxy "$PROFILE"
  fi
  if [ -d "${HERMES_HOME}/profiles" ]; then
    for _amp_dir in "${HERMES_HOME}/profiles"/*; do
      [ -d "$_amp_dir" ] || continue
      _amp_name="$(basename "$_amp_dir")"
      [ "$_amp_name" = "$PROFILE" ] && continue
      configure_model_proxy "$_amp_name"
    done
  fi
}

# Keep named tools in the model-visible schema when tool_search is active.
# Local models often describe-then-stop on deferred MCP tools; pinning avoids
# the tool_search → tool_describe → tool_call three-step for stack paths we
# actually route to (SearXNG, LightRAG, CalDAV, OpenCode, session_search).
#
# Usage: pin_tool_search_always_include <profile> <tool-name...>
pin_tool_search_always_include() {
  _ptai_profile="$1"
  shift
  [ "$#" -gt 0 ] || return 0
  log "Pinning tools in tools.tool_search.always_include for '${_ptai_profile:-default}'"
  ensure_yaml_list_items "$(config_path_for "$_ptai_profile")" \
    "tools.tool_search.always_include" "$@"
}

# Pin both short tool names and mcp__<server_key>__<tool> forms.
# Usage: pin_mcp_tools <profile> <server_key> <tool...>
pin_mcp_tools() {
  _pmt_profile="$1"
  _pmt_key="$2"
  shift 2
  [ "$#" -gt 0 ] || return 0
  _pmt_args=""
  for _pmt_t in "$@"; do
    _pmt_args="${_pmt_args} ${_pmt_t} mcp__${_pmt_key}__${_pmt_t}"
  done
  # shellcheck disable=SC2086
  pin_tool_search_always_include "$_pmt_profile" $_pmt_args
}

# Hermes v2026.9+ ignores tools.tool_search.always_include for MCP tools
# (toolset mcp-* always defers). The stack overlay
# patch-tool-search-always-include.py honors that list so SearXNG / LightRAG /
# OpenCode pins stay in the model-visible schema. tools.tool_search.defer is
# still the live knob for core tools: unset = curated default (includes
# session_search); an explicit list replaces that default wholesale;
# [] = defer no core tools.
#
# Usage: undefer_session_search <profile>
undefer_session_search() {
  _uss_profile="$1"
  log "Removing session_search from tools.tool_search.defer on '${_uss_profile:-default}'"
  _out="$(python3 - "$(config_path_for "$_uss_profile")" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, "/opt/hermes")
try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import sys as _sys
    _sys.path.insert(0, "/opt/hermes")
    import hermes_yaml as yaml
from utils import atomic_yaml_write

cfg_path = Path(sys.argv[1])
parent = cfg_path.parent
profile = parent.name if parent.parent.name == "profiles" else "default"
label = f"tools.tool_search.defer ({profile})"

try:
    from tools.tool_search import _DEFAULT_DEFERRED_TOOLS
    curated = set(_DEFAULT_DEFERRED_TOOLS)
except Exception:
    curated = {
        "computer_use", "session_search", "image_generate",
        "todo_list", "process_manage", "cronjob_manage",
        "drive_preview", "gui_tour", "desktop_preview", "annotate_preview",
        "show_tip", "setup_mcp", "desktop_project", "close_terminal",
        "apply_layout", "read_terminal", "read_window_below", "focus_pane",
    }

try:
    with open(cfg_path) as fh:
        config = yaml.safe_load(fh) or {}
except FileNotFoundError:
    print(f"ERROR: {cfg_path} not found")
    raise SystemExit(1)

tools = config.get("tools")
if not isinstance(tools, dict):
    tools = {}
    config["tools"] = tools
ts = tools.get("tool_search")
if not isinstance(ts, dict):
    ts = {}
    tools["tool_search"] = ts
current = ts.get("defer")

if current == []:
    print(f"{label} is [] (no core tools deferred) — session_search already eager")
    raise SystemExit(0)

if isinstance(current, list):
    next_list = [n for n in current if n != "session_search"]
    if next_list == current:
        print(f"{label} already omits session_search")
        raise SystemExit(0)
    ts["defer"] = next_list
    atomic_yaml_write(cfg_path, config)
    print(f"Removed session_search from {label}")
    raise SystemExit(0)

# Unset / scalar: write curated default minus session_search.
ts["defer"] = sorted(curated - {"session_search"})
atomic_yaml_write(cfg_path, config)
print(f"Set {label} to curated default minus session_search")
PY
)"
  if [ -n "$_out" ]; then log "$_out"; fi
}

# Re-pin the canonical eager set for a profile after every hermes config set.
# Incremental pin_mcp_tools calls can still lose items if a write races; this
# heals the list in one merge at the end of bootstrap.
#
# Usage: reconcile_stack_eager_tools <profile>
reconcile_stack_eager_tools() {
  _rst_profile="$1"
  _rst_args="session_search web_url_read searxng_web_search mcp__searxng__web_url_read mcp__searxng__searxng_web_search"
  case "$LIGHTRAG_MCP_ENABLED" in
    1|true|TRUE|yes|YES)
      if [ -n "$_rst_profile" ]; then
        _rst_args="${_rst_args} query_document get_documents get_pipeline_status get_graph_labels check_lightrag_health mcp__lightrag__query_document mcp__lightrag__get_documents mcp__lightrag__get_pipeline_status mcp__lightrag__get_graph_labels mcp__lightrag__check_lightrag_health"
      fi
      ;;
  esac
  case "$SQL_MCP_ENABLED" in
    1|true|TRUE|yes|YES)
      for _rst_t in $SQL_MCP_TOOLS; do
        _rst_args="${_rst_args} ${_rst_t} mcp__sql__${_rst_t}"
      done
      ;;
  esac
  if vikunja_mcp_token_for "$_rst_profile" >/dev/null; then
    for _rst_t in $(vikunja_mcp_pins_for "$_rst_profile"); do
      _rst_args="${_rst_args} ${_rst_t} mcp__${VIKUNJA_MCP_KEY}__${_rst_t}"
    done
  fi
  case "$MAIL_MCP_ENABLED" in
    1|true|TRUE|yes|YES)
      if [ "$_rst_profile" != "api-server" ]; then
        for _rst_t in $MAIL_MCP_TOOLS; do
          _rst_args="${_rst_args} ${_rst_t} mcp__mail__${_rst_t}"
        done
      fi
      ;;
  esac
  case "$MAIL_DRAFTS_ENABLED" in
    1|true|TRUE|yes|YES)
      if [ "$_rst_profile" != "api-server" ]; then
        for _rst_t in $MAIL_DRAFTS_MCP_TOOLS; do
          _rst_args="${_rst_args} ${_rst_t} mcp__drafts__${_rst_t}"
        done
      fi
      ;;
  esac
  case "$OPENCODE_MCP_ENABLED" in
    1|true|TRUE|yes|YES)
      if [ "$_rst_profile" != "api-server" ]; then
        _rst_args="${_rst_args} coding_list_roots coding_start_task coding_get_task_status coding_wait_for_task coding_get_task_result coding_continue_task mcp__opencode__coding_list_roots mcp__opencode__coding_start_task mcp__opencode__coding_get_task_status mcp__opencode__coding_wait_for_task mcp__opencode__coding_get_task_result mcp__opencode__coding_continue_task"
      fi
      ;;
  esac
  log "Reconciling tools.tool_search.always_include for '${_rst_profile:-default}'"
  # shellcheck disable=SC2086
  pin_tool_search_always_include "$_rst_profile" $_rst_args
  if [ -z "$_rst_profile" ] || [ "$_rst_profile" = "browser" ] || [ "$API_TOOLSET" = "hermes-cli" ]; then
    undefer_session_search "$_rst_profile"
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
try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import sys as _sys
    _sys.path.insert(0, "/opt/hermes")
    import hermes_yaml as yaml
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
  pin_mcp_tools "$_rwms_profile" searxng web_url_read searxng_web_search
}

# OpenCode coding delegation (coding compose profile). Mutating — never on
# api-server. Register on the default (dashboard/CLI) profile and on browser.
register_opencode_mcp_servers() {
  _roms_profile="$1"
  case "$OPENCODE_MCP_ENABLED" in
    1|true|TRUE|yes|YES) ;;
    *)
      log "Skipping OpenCode MCP registration (OPENCODE_MCP_ENABLED=${OPENCODE_MCP_ENABLED})"
      return 0
      ;;
  esac
  # Unattended n8n must not get write access to coding roots.
  if [ "$_roms_profile" = "api-server" ]; then
    log "Skipping OpenCode MCP on api-server profile (mutating coding tools)"
    return 0
  fi
  register_mcp_server "$_roms_profile" opencode OpenCode \
    "$OPENCODE_MCP_URL" "$OPENCODE_MCP_TIMEOUT" "$OPENCODE_MCP_CONNECT_TIMEOUT" \
    coding_list_roots coding_start_task coding_get_task_status \
    coding_wait_for_task coding_get_task_result coding_continue_task
  hermes_config_set "$_roms_profile" "mcp_servers.opencode.transport" "sse"
  # MCP tools are deferred behind tool_describe/tool_call when the schema budget
  # is tight. WebUI smoke showed the model describing coding_start_task then
  # stopping (finish_reason=stop) without invoking it. Pin the coding tools so
  # they stay in the model-visible tool list.
  pin_mcp_tools "$_roms_profile" opencode \
    coding_list_roots coding_start_task coding_get_task_status \
    coding_wait_for_task coding_get_task_result coding_continue_task
}

# Remove MCP server entries on one profile whose URL matches <url>, except
# <keep-key>. Unlike drop_unfiltered_mcp_duplicate this ignores tool filters:
# for the SQL endpoints the boundary is the database role behind the URL, so a
# filtered entry pointing at the read/write sidecar is still read/write.
#
# Usage: drop_mcp_entries_for_url <profile> <keep-key> <url>
drop_mcp_entries_for_url() {
  _out="$(python3 - "$(config_path_for "$1")" "$2" "$3" <<'PY'
import sys
sys.path.insert(0, "/opt/hermes")
try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import sys as _sys
    _sys.path.insert(0, "/opt/hermes")
    import hermes_yaml as yaml
from utils import atomic_yaml_write

cfg_path, keep_key, url = sys.argv[1:4]

try:
    with open(cfg_path) as fh:
        config = yaml.safe_load(fh) or {}
except FileNotFoundError:
    raise SystemExit(0)

servers = config.get("mcp_servers")
if not isinstance(servers, dict):
    raise SystemExit(0)

target = url.rstrip("/")
removed = [
    key for key, entry in servers.items()
    if key != keep_key
    and isinstance(entry, dict)
    and str(entry.get("url", "")).rstrip("/") == target
]
for key in removed:
    del servers[key]
if removed:
    atomic_yaml_write(cfg_path, config)
    print(f"Removed MCP server(s) {', '.join(sorted(removed))} pointing at {url}")
PY
)"
  if [ -n "$_out" ]; then log "$_out"; fi
}

# Shared Postgres SQL MCP (db compose profile), registered as mcp_servers.sql.
# api-server gets the agent_ro sidecar (read everything, write nothing);
# default and browser get agent_rw (same reads, plus CREATE/DML in schema
# agent). Same read/write split as CalDAV, but Postgres grants enforce it
# rather than a tool allowlist.
#
# Profiles are cloned from default, so api-server starts with default's rw
# entry. Re-registering the sql key moves it to ro, and any other entry that
# still points at the rw URL (a hand-added copy under another key) is dropped.
register_sql_mcp_servers() {
  _rsms_profile="$1"
  case "$SQL_MCP_ENABLED" in
    1|true|TRUE|yes|YES) ;;
    *)
      log "Skipping SQL MCP registration (SQL_MCP_ENABLED=${SQL_MCP_ENABLED})"
      # Undo an earlier registration. With db off nothing answers at these
      # URLs, and every session would wait out connect_timeout on them.
      drop_mcp_entries_for_url "$_rsms_profile" "" "$SQL_MCP_RO_URL"
      drop_mcp_entries_for_url "$_rsms_profile" "" "$SQL_MCP_RW_URL"
      return 0
      ;;
  esac
  if [ "$_rsms_profile" = "api-server" ]; then
    _rsms_url="$SQL_MCP_RO_URL"
    _rsms_display="SQL (read-only, agent_ro)"
  else
    _rsms_url="$SQL_MCP_RW_URL"
    _rsms_display="SQL (agent_rw)"
  fi
  # shellcheck disable=SC2086
  register_mcp_server "$_rsms_profile" sql "$_rsms_display" \
    "$_rsms_url" "$SQL_MCP_TIMEOUT" "$SQL_MCP_CONNECT_TIMEOUT" $SQL_MCP_TOOLS
  hermes_config_set "$_rsms_profile" "mcp_servers.sql.transport" "sse"
  if [ "$_rsms_profile" = "api-server" ]; then
    drop_mcp_entries_for_url "$_rsms_profile" sql "$SQL_MCP_RW_URL"
  fi
  # shellcheck disable=SC2086
  pin_mcp_tools "$_rsms_profile" sql $SQL_MCP_TOOLS
}

# Vikunja token for a profile: read-only on api-server, read/write elsewhere.
# Prints nothing and fails when the todo profile is off or the token is unset.
vikunja_mcp_token_for() {
  case "$VIKUNJA_MCP_ENABLED" in
    1|true|TRUE|yes|YES) ;;
    *) return 1 ;;
  esac
  if [ "$1" = "api-server" ]; then
    _vmt_token="$VIKUNJA_MCP_TOKEN_RO"
  else
    _vmt_token="$VIKUNJA_MCP_TOKEN_RW"
  fi
  [ -n "$_vmt_token" ] || return 1
  printf '%s\n' "$_vmt_token"
}

vikunja_mcp_pins_for() {
  if [ "$1" = "api-server" ]; then
    printf '%s\n' "$VIKUNJA_MCP_PIN_RO"
  else
    printf '%s\n' "$VIKUNJA_MCP_PIN_RW"
  fi
}

# Vikunja todo lists (todo compose profile), registered as mcp_servers.todos
# (VIKUNJA_MCP_KEY). Before that key existed it was mcp_servers.vikunja; the
# same-URL cleanup below removes that entry.
# The boundary is the token's scopes, which Vikunja enforces server-side (tools
# outside them are neither listed nor callable), so no tools.include here.
# make todo-init mints both tokens: api-server gets the read-only one, default
# and browser the read/write one. Same split as SQL and CalDAV.
#
# The token goes into the profile's .env as VIKUNJA_MCP_TOKEN, and config.yaml
# holds only the ${VIKUNJA_MCP_TOKEN} reference. Hermes resolves it per profile.
register_vikunja_mcp_servers() {
  _rvms_profile="$1"
  _rvms_env="$(caldav_profile_env_file "$_rvms_profile")"
  if ! _rvms_token="$(vikunja_mcp_token_for "$_rvms_profile")"; then
    case "$VIKUNJA_MCP_ENABLED" in
      1|true|TRUE|yes|YES)
        log "WARNING: VIKUNJA_MCP_ENABLED=1 but no token for '${_rvms_profile:-default}' - run make todo-init"
        ;;
      *)
        log "Skipping Vikunja MCP registration (VIKUNJA_MCP_ENABLED=${VIKUNJA_MCP_ENABLED})"
        ;;
    esac
    # Undo an earlier registration so sessions do not wait out connect_timeout.
    drop_mcp_entries_for_url "$_rvms_profile" "" "$VIKUNJA_MCP_URL"
    remove_env_key "$_rvms_env" VIKUNJA_MCP_TOKEN
    return 0
  fi
  mkdir -p "$(dirname "$_rvms_env")"
  upsert_env "$_rvms_env" VIKUNJA_MCP_TOKEN "$_rvms_token"
  log "Registering todo lists (Vikunja) MCP server '${VIKUNJA_MCP_KEY}' for profile '${_rvms_profile:-default}' at ${VIKUNJA_MCP_URL}"
  # hermes config set cannot write a header map.
  _out="$(python3 - "$(config_path_for "$_rvms_profile")" "$VIKUNJA_MCP_KEY" "$VIKUNJA_MCP_URL" \
    "$VIKUNJA_MCP_TIMEOUT" "$VIKUNJA_MCP_CONNECT_TIMEOUT" <<'PY'
import sys
sys.path.insert(0, "/opt/hermes")
try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import sys as _sys
    _sys.path.insert(0, "/opt/hermes")
    import hermes_yaml as yaml
from utils import atomic_yaml_write

cfg_path, key, url, timeout, connect_timeout = sys.argv[1:6]

try:
    with open(cfg_path) as fh:
        config = yaml.safe_load(fh) or {}
except FileNotFoundError:
    print(f"ERROR: {cfg_path} not found")
    raise SystemExit(1)

servers = config.get("mcp_servers")
if not isinstance(servers, dict):
    servers = {}
    config["mcp_servers"] = servers
entry = servers.get(key)
if not isinstance(entry, dict):
    entry = {}
entry["url"] = url
entry["timeout"] = int(timeout) if str(timeout).isdigit() else timeout
entry["connect_timeout"] = int(connect_timeout) if str(connect_timeout).isdigit() else connect_timeout
headers = entry.get("headers")
if not isinstance(headers, dict):
    headers = {}
headers["Authorization"] = "Bearer ${VIKUNJA_MCP_TOKEN}"
entry["headers"] = headers
servers[key] = entry
atomic_yaml_write(cfg_path, config)
print(f"Wrote mcp_servers.{key}")
PY
)"
  if [ -n "$_out" ]; then log "$_out"; fi
  # One entry per profile: drops the pre-rename mcp_servers.vikunja, and on
  # api-server any hand-added copy that could carry a read/write token.
  drop_mcp_entries_for_url "$_rvms_profile" "$VIKUNJA_MCP_KEY" "$VIKUNJA_MCP_URL"
  # shellcheck disable=SC2046
  pin_mcp_tools "$_rvms_profile" "$VIKUNJA_MCP_KEY" $(vikunja_mcp_pins_for "$_rvms_profile")
}

# Read-only mail index (mail compose profile), registered as mcp_servers.mail
# on the interactive profiles only. Mail is the easiest place for a stranger
# to plant instructions, so unattended api-server sessions never get it, not
# even read-only. api-server is cloned from default, so the inherited entry
# (and any hand-added copy pointing at the same URL) is removed there.
register_mail_mcp_servers() {
  _rmms_profile="$1"
  case "$MAIL_MCP_ENABLED" in
    1|true|TRUE|yes|YES) ;;
    *)
      log "Skipping mail MCP registration (MAIL_MCP_ENABLED=${MAIL_MCP_ENABLED})"
      drop_mcp_entries_for_url "$_rmms_profile" "" "$MAIL_MCP_URL"
      return 0
      ;;
  esac
  if [ "$_rmms_profile" = "api-server" ]; then
    log "Skipping mail MCP on api-server profile (unattended sessions)"
    drop_mcp_entries_for_url "$_rmms_profile" "" "$MAIL_MCP_URL"
    return 0
  fi
  # shellcheck disable=SC2086
  register_mcp_server "$_rmms_profile" mail "Mail (read-only)" \
    "$MAIL_MCP_URL" "$MAIL_MCP_TIMEOUT" "$MAIL_MCP_CONNECT_TIMEOUT" $MAIL_MCP_TOOLS
  # shellcheck disable=SC2086
  pin_mcp_tools "$_rmms_profile" mail $MAIL_MCP_TOOLS
}

# Draft-only mail MCP: queues drafts the host helper saves in Mail. Like the
# read MCP, never on api-server: drafting is something a person asks for.
register_mail_drafts_mcp_servers() {
  _rmdm_profile="$1"
  case "$MAIL_DRAFTS_ENABLED" in
    1|true|TRUE|yes|YES) ;;
    *)
      log "Skipping mail drafts MCP registration (MAIL_DRAFTS_ENABLED=${MAIL_DRAFTS_ENABLED})"
      drop_mcp_entries_for_url "$_rmdm_profile" "" "$MAIL_DRAFTS_MCP_URL"
      return 0
      ;;
  esac
  if [ "$_rmdm_profile" = "api-server" ]; then
    log "Skipping mail drafts MCP on api-server profile (unattended sessions)"
    drop_mcp_entries_for_url "$_rmdm_profile" "" "$MAIL_DRAFTS_MCP_URL"
    return 0
  fi
  # shellcheck disable=SC2086
  register_mcp_server "$_rmdm_profile" drafts "Mail drafts (no sending)" \
    "$MAIL_DRAFTS_MCP_URL" "$MAIL_DRAFTS_MCP_TIMEOUT" "$MAIL_DRAFTS_MCP_CONNECT_TIMEOUT" $MAIL_DRAFTS_MCP_TOOLS
  # shellcheck disable=SC2086
  pin_mcp_tools "$_rmdm_profile" drafts $MAIL_DRAFTS_MCP_TOOLS
}

# Keep the mail tools away from cron jobs that didn't ask for them.
#
# Cron runs unattended, and a job without its own tool list gets every MCP
# server, mail included, next to web search, the browser and a shell. While
# mail or drafts is registered on a profile, pin platform_toolsets.cron to
# Hermes's default cron tools plus every other MCP server. A job that names
# mail in its own enabled_toolsets (make mail-triage-cron) still gets it.
#
# A list we wrote is recorded in .mail-cron-toolsets.json next to config.yaml,
# so it can be refreshed or removed later. A list you wrote yourself is kept;
# only mail and drafts are taken out of it.
fence_mail_from_cron() {
  _fmc_cfg="$(config_path_for "$1")"
  [ -f "$_fmc_cfg" ] || return 0
  _out="$(python3 - "$_fmc_cfg" <<'PY'
import json
import sys
from pathlib import Path

sys.path.insert(0, "/opt/hermes")
try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import sys as _sys
    _sys.path.insert(0, "/opt/hermes")
    import hermes_yaml as yaml
from utils import atomic_yaml_write

MAIL_URLS = ("mail-mcp:8000", "mail-drafts-mcp:8000")
cfg_path = Path(sys.argv[1])
state_path = cfg_path.parent / ".mail-cron-toolsets.json"
profile = cfg_path.parent.name if cfg_path.parent.parent.name == "profiles" else "default"

config = yaml.safe_load(cfg_path.read_text()) or {}
servers = config.get("mcp_servers") or {}
enabled = {str(n) for n, s in servers.items()
           if isinstance(s, dict) and str(s.get("enabled", True)).lower() not in ("false", "no", "off", "0")}
mail = {n for n in enabled if any(u in str(servers[n].get("url", "")) for u in MAIL_URLS)}
platform = config.get("platform_toolsets")
platform = platform if isinstance(platform, dict) else {}
current = platform.get("cron")
try:
    managed = json.loads(state_path.read_text()) == current
except (OSError, ValueError):
    managed = False
others = sorted(enabled - mail) or ["no_mcp"]

if not mail:
    if managed and current is not None:
        platform.pop("cron", None)
        config["platform_toolsets"] = platform
        atomic_yaml_write(str(cfg_path), config)
        state_path.unlink(missing_ok=True)
        print(f"removed the cron tool list added for mail ({profile})")
    raise SystemExit(0)

if current is None or managed:
    desired = ["hermes-cron"] + others
else:
    desired = [t for t in current if t not in mail]
    if not set(desired) & (enabled | {"no_mcp"}):
        # A list naming no MCP server gets all of them, mail included.
        desired += others
if desired == current:
    print(f"cron jobs on {profile} already can't reach mail unless they ask")
    raise SystemExit(0)
platform["cron"] = desired
config["platform_toolsets"] = platform
atomic_yaml_write(str(cfg_path), config)
if current is None or managed:
    state_path.write_text(json.dumps(desired))
    print(f"pinned platform_toolsets.cron on {profile} without mail: {', '.join(desired)}")
else:
    print(f"took mail out of your platform_toolsets.cron on {profile}")
PY
)"
  if [ -n "$_out" ]; then log "$_out"; fi
}

# Slug personal -> CALDAV_PERSONAL_PASSWORD. Hyphens become underscores.
caldav_password_env_key() {
  _cpek_slug="$1"
  _cpek_name="$(printf '%s' "$_cpek_slug" | tr '[:lower:]-' '[:upper:]_')"
  printf 'CALDAV_%s_PASSWORD' "$_cpek_name"
}

# Default profile secrets live in HERMES_HOME/.env. Named profiles do not
# inherit that file, so each registration writes its own copy.
caldav_profile_env_file() {
  if [ -z "$1" ]; then
    printf '%s\n' "$DEFAULT_ENV"
  else
    printf '%s/profiles/%s/.env\n' "$HERMES_HOME" "$1"
  fi
}

# Register one CalDAV account as mcp_servers.caldav-<slug> with X-Caldav-* headers.
# hermes config set cannot write nested header maps, so this uses atomic_yaml_write.
# The password argument is a ${CALDAV_<SLUG>_PASSWORD} reference, not the secret.
#
# Usage: register_caldav_account <profile> <slug> <url> <username> <password-ref> <timeout> <connect_timeout> [tool...]
register_caldav_account() {
  _rca_profile="$1"
  _rca_slug="$2"
  _rca_url="$3"
  _rca_user="$4"
  _rca_pass_ref="$5"
  _rca_timeout="$6"
  _rca_connect="$7"
  shift 7
  _rca_key="caldav-${_rca_slug}"
  _rca_cfg="$(config_path_for "$_rca_profile")"
  log "Registering CalDAV MCP server '${_rca_key}' for profile '${_rca_profile:-default}'"
  _out="$(python3 - "$_rca_cfg" "$_rca_key" "$CALDAV_MCP_URL" "$_rca_url" "$_rca_user" "$_rca_pass_ref" \
    "$_rca_timeout" "$_rca_connect" "$@" <<'PY'
import sys
sys.path.insert(0, "/opt/hermes")
try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import sys as _sys
    _sys.path.insert(0, "/opt/hermes")
    import hermes_yaml as yaml
from utils import atomic_yaml_write

cfg_path, key, mcp_url, caldav_url, username, password_ref, timeout, connect_timeout = sys.argv[1:9]
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
    "X-Caldav-Password": password_ref,
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
  if [ "$#" -gt 0 ]; then
    pin_mcp_tools "$_rca_profile" "$_rca_key" "$@"
  fi
}
# Usage: reconcile_caldav_accounts <profile> <slug...>
reconcile_caldav_accounts() {
  _rec_profile="$1"
  shift
  _rec_cfg="$(config_path_for "$_rec_profile")"
  _out="$(python3 - "$_rec_cfg" "$@" <<'PY'
import sys
sys.path.insert(0, "/opt/hermes")
try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import sys as _sys
    _sys.path.insert(0, "/opt/hermes")
    import hermes_yaml as yaml
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
    removed.append(key[len("caldav-"):])

if removed:
    atomic_yaml_write(cfg_path, config)
    print("Removed stale CalDAV MCP entries: " + ", ".join("caldav-" + s for s in sorted(removed)))
    for slug in sorted(removed):
        print("REMOVED_SLUG " + slug)
PY
)"
  if [ -n "$_out" ]; then
    _rec_env="$(caldav_profile_env_file "$_rec_profile")"
    printf '%s\n' "$_out" | while IFS= read -r _rec_line; do
      case "$_rec_line" in
        REMOVED_SLUG\ *)
          _rec_slug="${_rec_line#REMOVED_SLUG }"
          _rec_key="$(caldav_password_env_key "$_rec_slug")"
          remove_env_key "$_rec_env" "$_rec_key"
          log "Removed ${_rec_key} from ${_rec_env}"
          ;;
        *)
          log "$_rec_line"
          ;;
      esac
    done
  fi
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

    _rcms_env_key="$(caldav_password_env_key "$_rcms_slug")"
    _rcms_pass_ref="\${${_rcms_env_key}}"
    _rcms_env_file="$(caldav_profile_env_file "$_rcms_profile")"
    mkdir -p "$(dirname "$_rcms_env_file")"
    upsert_env "$_rcms_env_file" "$_rcms_env_key" "$_rcms_pass"
    register_caldav_account "$_rcms_profile" "$_rcms_slug" \
      "$_rcms_url" "$_rcms_user" "$_rcms_pass_ref" \
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

# Narrated intent ("I'll …") with finish_reason=stop and no tool call.
# Browser sets this on its own profile. Default serves Boundary, the
# dashboard, and the CLI. Leave api-server alone (unattended turn cap).
log "Setting agent.intent_ack_continuation=true on default profile"
hermes_config_set "" "agent.intent_ack_continuation" "true"

# --- Register web-reading MCP servers (default profile) ---
# Same rationale as the skills registration: dashboard and CLI sessions get URL
# reading whether or not the API server profile is ever configured.
register_web_mcp_servers ""
register_caldav_mcp_servers ""
register_opencode_mcp_servers ""
register_sql_mcp_servers ""
register_vikunja_mcp_servers ""
register_mail_mcp_servers ""
register_mail_drafts_mcp_servers ""

# session_search is on Hermes' curated tool_search.defer list. Pin + undefer
# on default before the api-server.env gate so a missing env file cannot
# leave MCP pins without recall.
pin_tool_search_always_include "" session_search
undefer_session_search ""

# Auto skill-library review after a finished (or halted) turn forks bg-review
# and can hold the LLM stream for hours. Interactive users create skills
# explicitly; memory review stays on via memory.nudge_interval.
log "Setting skills.creation_nudge_interval=0 on default profile"
hermes_config_set "" "skills.creation_nudge_interval" "0"

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
# todos go through the living-todos skill (Vikunja when the todo profile is on,
# else /opt/projects/project-todo-list).
#
# Also disable the native `web` toolset (web_search / web_extract). With
# SEARXNG_URL set, Hermes auto-binds both to SearXNG: search works but extract
# always fails ("search-only backend"), and models ignore soft routing and spray
# parallel native web_search calls that empty upstream engines. MCP searxng
# (searxng_web_search + web_url_read) is the supported path on this stack.
log "Disabling session-only todo and native web toolsets on default and '${PROFILE}' profiles"
ensure_yaml_list_items "$(config_path_for "")" "agent.disabled_toolsets" "todo" "web"
ensure_yaml_list_items "$(config_path_for "$PROFILE")" "agent.disabled_toolsets" "todo" "web"

if [ "$API_TOOLSET" = "hermes-cli" ] || [ "$PROFILE" = "browser" ]; then
  pin_tool_search_always_include "$PROFILE" session_search
  undefer_session_search "$PROFILE"
  log "Setting skills.creation_nudge_interval=0 on profile '${PROFILE}'"
  hermes_config_set "$PROFILE" "skills.creation_nudge_interval" "0"
fi

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

# Cap browser/WebUI completions so a local model cannot dump a 250k
# "let me fetch this URL" narration after a successful tool. 1024 tokens
# is enough for a featured-article title and list-projects / remember.
if [ "$PROFILE" = "browser" ]; then
  log "Setting model.max_tokens=1024 on browser (cap runaway final answers)"
  hermes_config_set "$PROFILE" "model.max_tokens" "1024"
fi

# Hermes treats the dotless llm-proxy host as local whatever sits behind it,
# so Hermes gives every profile its local stale-stream timeout (900s) even
# for a cloud model. Size it for the real upstream instead.
if upstream_is_local "$LLM_BINDING_HOST"; then
  UPSTREAM_KIND=local
  STALE_DEFAULT=300
else
  UPSTREAM_KIND=cloud
  STALE_DEFAULT=180
fi
STALE_TIMEOUT="${PROXY_STALE_TIMEOUT:-$STALE_DEFAULT}"
# Applied to every profile by apply_model_proxy_all_profiles at the end.
log "Stale-stream timeout ${STALE_TIMEOUT}s for every profile (${UPSTREAM_KIND} upstream)"

# Auto-title starts with the user's turn and uses the same model. A local
# server that runs one request at a time serves the title first, and the
# reply waits behind it.
if [ "$PROFILE" = "browser" ]; then
  case "$BROWSER_AUTO_TITLE" in
    1|true|TRUE|yes|YES) TITLE_ENABLED=true ;;
    0|false|FALSE|no|NO) TITLE_ENABLED=false ;;
    *) if [ "$UPSTREAM_KIND" = local ]; then TITLE_ENABLED=false; else TITLE_ENABLED=true; fi ;;
  esac
  log "Setting auxiliary.title_generation.enabled=${TITLE_ENABLED} on browser (${UPSTREAM_KIND} upstream)"
  hermes_config_set "$PROFILE" "auxiliary.title_generation.enabled" "$TITLE_ENABLED"
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
    pin_mcp_tools "$PROFILE" lightrag \
      query_document get_documents get_pipeline_status get_graph_labels check_lightrag_health
    ;;
  *)
    log "Skipping LightRAG MCP registration (LIGHTRAG_MCP_ENABLED=${LIGHTRAG_MCP_ENABLED})"
    ;;
esac

# Same web-reading servers as the default profile above; profiles carry their
# own config.yaml, so the earlier registration does not reach API sessions.
register_web_mcp_servers "$PROFILE"
register_caldav_mcp_servers "$PROFILE"
register_opencode_mcp_servers "$PROFILE"
register_sql_mcp_servers "$PROFILE"
register_vikunja_mcp_servers "$PROFILE"
register_mail_mcp_servers "$PROFILE"
register_mail_drafts_mcp_servers "$PROFILE"

# Heal always_include + defer after every hermes config set / MCP register.
reconcile_stack_eager_tools ""
reconcile_stack_eager_tools "$PROFILE"

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

try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import sys as _sys
    _sys.path.insert(0, "/opt/hermes")
    import hermes_yaml as yaml

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

fence_mail_from_cron ""
fence_mail_from_cron "$PROFILE"

apply_terminal_backend_all_profiles
apply_model_proxy_all_profiles

log "Done: profile=${PROFILE} port=${API_PORT} max_turns=${MAX_TURNS} toolset=${API_TOOLSET}"
log "API URL (host): http://localhost:${API_PORT}/v1"
log "API URL (compose): http://hermes:${API_PORT}/v1"
