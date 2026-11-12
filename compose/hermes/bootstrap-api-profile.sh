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
TOOL_LOOP_HARD_STOP="${HERMES_TOOL_LOOP_HARD_STOP:-true}"
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
SEARXNG_MCP_URL="${SEARXNG_MCP_URL:-http://mcp-searxng:3000/mcp}"
SEARXNG_MCP_TIMEOUT="${SEARXNG_MCP_TIMEOUT:-60}"
SEARXNG_MCP_CONNECT_TIMEOUT="${SEARXNG_MCP_CONNECT_TIMEOUT:-30}"
OPENCODE_MCP_URL="${OPENCODE_MCP_URL:-http://opencode-mcp:8000/sse}"
OPENCODE_MCP_TIMEOUT="${OPENCODE_MCP_TIMEOUT:-600}"
OPENCODE_MCP_CONNECT_TIMEOUT="${OPENCODE_MCP_CONNECT_TIMEOUT:-30}"
OPENCODE_MCP_ENABLED="${OPENCODE_MCP_ENABLED:-0}"
# The mail and drafts servers register from addons/email-local/addon.json,
# which reads MAIL_MCP_ENABLED, MAIL_DRAFTS_ENABLED and the MAIL_*_URL and
# timeout overrides from the environment. The URLs stay here for
# set_stack_mcp_parked_retry.
MAIL_MCP_URL="${MAIL_MCP_URL:-http://mail-mcp:8000/mcp}"
MAIL_DRAFTS_MCP_URL="${MAIL_DRAFTS_MCP_URL:-http://mail-drafts-mcp:8000/mcp}"
MESSAGES_MCP_ENABLED="${MESSAGES_MCP_ENABLED:-0}"
MESSAGES_MCP_URL="${MESSAGES_MCP_URL:-http://messages-mcp:8000/mcp}"
MESSAGES_MCP_TIMEOUT="${MESSAGES_MCP_TIMEOUT:-60}"
MESSAGES_MCP_CONNECT_TIMEOUT="${MESSAGES_MCP_CONNECT_TIMEOUT:-30}"
MESSAGES_MCP_TOOLS="messages_status messages_chats messages_search messages_conversation messages_get"
# Built-in add-ons (GPT Researcher so far) come from addons/<id>/addon.json.
ADDONS_DIR="${ADDONS_DIR:-/bootstrap/addons}"
ADDON_MANIFEST_PY="${ADDON_MANIFEST_PY:-/bootstrap/addon_manifest.py}"
# Stores of add-ons with mcp.instances, one <short id>/ each (Twenty's
# workspaces, Actual's budgets).
ADDON_DATA_DIR="${ADDON_DATA_DIR:-/bootstrap/addon-data}"
# Probe interval for a parked stack MCP server (patch-mcp-wake.py; Hermes
# default 300, floor 10). hermes starts without waiting for its MCP sidecars,
# so one still booting parks and comes back on the next probe. Empty leaves
# the Hermes default.
MCP_PARKED_RETRY_INTERVAL="${MCP_PARKED_RETRY_INTERVAL-15}"
# messages_triage_batch moves the triage cursor, so only the default profile,
# where the messages-triage cron job runs, registers it, and it isn't pinned.
MESSAGES_TRIAGE_TOOL="messages_triage_batch"
# Shared with messages-mcp's forwarder; the webhook route checks it.
MESSAGES_WEBHOOK_SECRET="${MESSAGES_WEBHOOK_SECRET:-}"
MESSAGES_WEBHOOK_PORT="${MESSAGES_WEBHOOK_PORT:-8646}"
# llm-proxy's real upstream. Only used to tell a local model server from a
# cloud API; the key never reaches this container.
LLM_BINDING_HOST="${LLM_BINDING_HOST:-http://host.docker.internal:1234/v1}"
# Empty = 300s for a local upstream, 180s for a cloud one.
PROXY_STALE_TIMEOUT="${HERMES_PROXY_STALE_TIMEOUT:-}"
# model.max_tokens for every profile on the proxy that has none. 0 = leave unset.
MODEL_MAX_TOKENS="${HERMES_MAX_TOKENS:-16384}"
# auto = off for a local upstream, on for a cloud one. 0/1 force it.
BROWSER_AUTO_TITLE="${HERMES_BROWSER_AUTO_TITLE:-auto}"

# The default profile and the sweep over every profile come out the same from
# either bootstrap, so only hermes-api-bootstrap runs them (1) and
# hermes-browser-bootstrap, which runs after it, does its own profile (0).
BOOTSTRAP_SHARED="${HERMES_BOOTSTRAP_SHARED:-1}"
# 1 runs the bootstrap even when its stamp matches.
BOOTSTRAP_FORCE="${HERMES_BOOTSTRAP_FORCE:-0}"

PROFILE_DIR="${HERMES_HOME}/profiles/${PROFILE}"
PROFILE_ENV="${PROFILE_DIR}/.env"
DEFAULT_ENV="${HERMES_HOME}/.env"
STAMP_DIR="${HERMES_HOME}/.bootstrap-stamps"

log() {
  printf '[hermes-api-bootstrap] %s\n' "$1"
}

is_true() {
  case "$1" in
    1|true|TRUE|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

sha256_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | cut -d' ' -f1
  else
    command python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
  fi
}

# Skipping an unchanged run
#
# Compose starts exited one-shots again on every `up`, so without this the full
# bootstrap runs on every stack start. A run is skipped when two hashes match
# what the last complete run recorded:
#
# - inputs: this script, the profile's env file, the
#   add-on instances (key files by name and mtime), the add-on manifests and their reader, the
#   image, and every environment variable this container gets. One stamp per
#   profile under STAMP_DIR.
# - state: every file the bootstrap writes, in the default profile and every
#   named profile. One stamp shared by both bootstraps, written by whichever
#   ran last. A config edited by hand or by Hermes, or a profile added or
#   removed, changes it, and the next start runs the full bootstrap again.
#
# gateway_state.json is left out of the state, since gateways rewrite it, and
# it is written on every start, skipped or not.
bootstrap_inputs_hash() {
  {
    echo "script"
    cat "$0"
    echo "source-env"
    cat "$SOURCE_ENV"
    echo "image ${HERMES_AGENT_IMAGE_TAG:-}"
    cat /opt/hermes/pyproject.toml 2>/dev/null || true
    # A new, removed or re-keyed add-on instance (a Twenty workspace) changes
    # what gets registered. Key files count by name and mtime, never content.
    echo "addon-instances"
    for _bih_f in "$ADDON_DATA_DIR"/*/instances/*.json; do
      [ -f "$_bih_f" ] || continue
      echo "$_bih_f"
      cat "$_bih_f"
    done
    for _bih_f in "$ADDON_DATA_DIR"/*/secrets/*; do
      [ -f "$_bih_f" ] || continue
      echo "$_bih_f $(stat -c %Y "$_bih_f" 2>/dev/null || stat -f %m "$_bih_f" 2>/dev/null || echo unknown)"
    done
    # A changed add-on manifest, or reader, changes what gets registered.
    echo "addons"
    cat "$ADDON_MANIFEST_PY" 2>/dev/null || true
    for _bih_f in "$ADDONS_DIR"/*/addon.json; do
      [ -f "$_bih_f" ] || continue
      echo "$_bih_f"
      cat "$_bih_f"
    done
    echo "env"
    env | grep -v -e '^HOSTNAME=' -e '^PWD=' -e '^OLDPWD=' -e '^SHLVL=' -e '^_=' | LC_ALL=C sort
  } | sha256_stdin
}

bootstrap_state_hash() {
  {
    for _bsh_home in "$HERMES_HOME" "$HERMES_HOME"/profiles/*; do
      [ -d "$_bsh_home" ] || continue
      echo "home ${_bsh_home}"
      for _bsh_f in config.yaml .env .messages-webhook.json .mail-cron-toolsets.json; do
        [ -f "${_bsh_home}/${_bsh_f}" ] || continue
        echo "file ${_bsh_f}"
        cat "${_bsh_home}/${_bsh_f}"
      done
    done
  } | sha256_stdin
}

# `hermes gateway start` is a no-op inside Docker ("Service start is not
# applicable inside a Docker container"). The s6 reconciler in the main hermes
# container reads each profile's gateway_state.json and only auto-starts when
# gateway_state is "running", so seed that file directly (same contract as
# HERMES_GATEWAY_BOOTSTRAP_STATE).
mark_gateway_running() {
  log "Marking gateway for profile '${PROFILE}' as running (gateway_state.json)"
  printf '{"gateway_state":"running"}\n' > "${PROFILE_DIR}/gateway_state.json"
  chmod 644 "${PROFILE_DIR}/gateway_state.json" 2>/dev/null || true
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
# Each `hermes config set` costs about a second of CLI start-up, and a full run
# sets around a hundred keys. So this only queues the key, and config_flush
# applies the queue in one Python process through set_config_value, the
# function the CLI calls (scripts/model-use.sh does the same). The python3
# wrapper below flushes before any other helper reads or writes config.yaml,
# which keeps every write in script order.
#
# sh has no local variables, so every helper here prefixes its own to avoid
# clobbering a caller's: this one runs inside register_mcp_server's loop.
CONFIG_QUEUE="$(mktemp)"
trap 'rm -f "$CONFIG_QUEUE"' EXIT
hermes_config_set() {
  printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$CONFIG_QUEUE"
}

# Apply queued keys. The queue is kept on failure, so the next flush retries
# it and the final flush at the end of the script exits non-zero.
config_flush() {
  [ -s "$CONFIG_QUEUE" ] || return 0
  command python3 - "$HERMES_HOME" "$CONFIG_QUEUE" <<'PY' || return 1
import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, "/opt/hermes")
from hermes_cli.config import set_config_value
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

root = Path(sys.argv[1])
counts = {}
failed = False
for line in Path(sys.argv[2]).read_text().splitlines():
    profile, key, value = line.split("\t", 2)
    token = set_hermes_home_override(root / "profiles" / profile if profile else root)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            set_config_value(key, value)
        counts[profile] = counts.get(profile, 0) + 1
    except BaseException as exc:  # the CLI exits on a rejected key
        print(f"[hermes-api-bootstrap] config set {key} failed on '{profile or 'default'}': {exc!r}",
              file=sys.stderr)
        failed = True
    finally:
        reset_hermes_home_override(token)
# stderr: a flush can run inside a helper whose stdout the caller captures.
for profile, count in counts.items():
    print(f"[hermes-api-bootstrap] Set {count} config key(s) on '{profile or 'default'}'",
          file=sys.stderr)
raise SystemExit(1 if failed else 0)
PY
  : > "$CONFIG_QUEUE"
}

python3() {
  config_flush || exit 1
  command python3 "$@"
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
# has no Node or browser. gateway keeps it in this container. Written as YAML
# because some Hermes versions reject the key in `hermes config set`.
set_bot_desktop_placement() {
  _sbdp_profile="$1"
  _sbdp_value="$2"
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
  if ! is_true "$BOOTSTRAP_SHARED"; then
    "$_atb_fn" "$PROFILE"
    return 0
  fi
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
  if ! is_true "$BOOTSTRAP_SHARED"; then
    configure_model_proxy "$PROFILE"
    return 0
  fi
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

# Warn-only is the Hermes default on api_server, which treats the client as
# someone able to stop a loop. A background turn has no such person.
apply_tool_loop_hard_stop_all_profiles() {
  if ! is_true "$BOOTSTRAP_SHARED"; then
    log "Setting tool_loop_guardrails.hard_stop_enabled=${TOOL_LOOP_HARD_STOP} on profile '${PROFILE}'"
    hermes_config_set "$PROFILE" tool_loop_guardrails.hard_stop_enabled "$TOOL_LOOP_HARD_STOP"
    return 0
  fi
  log "Setting tool_loop_guardrails.hard_stop_enabled=${TOOL_LOOP_HARD_STOP} on every profile"
  for _ths_profile in "" $(
    [ -d "${HERMES_HOME}/profiles" ] && ls "${HERMES_HOME}/profiles"
  ); do
    if [ -n "$_ths_profile" ] && [ ! -d "${HERMES_HOME}/profiles/${_ths_profile}" ]; then
      continue
    fi
    hermes_config_set "$_ths_profile" tool_loop_guardrails.hard_stop_enabled "$TOOL_LOOP_HARD_STOP"
  done
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
# The name Hermes registers an MCP tool under: mcp__<server>__<tool>, with each
# character outside [A-Za-z0-9_] replaced by _ (sanitize_mcp_name_component in
# tools/mcp_tool_schema.py). crm-main's tools are mcp__crm_main__*. A name
# over 64 characters is cut to 55 and given "_" and the first 8 hex digits of
# the full name's SHA-256, as mcp_prefixed_tool_name does, so its pin matches.
mcp_tool_name() {
  _mtn="mcp__$(printf '%s' "$1" | sed 's/[^A-Za-z0-9_]/_/g')__$(printf '%s' "$2" | sed 's/[^A-Za-z0-9_]/_/g')"
  if [ "${#_mtn}" -le 64 ]; then
    printf '%s' "$_mtn"
    return 0
  fi
  printf '%s_%s' "$(printf '%s' "$_mtn" | cut -c1-55)" \
    "$(printf '%s' "$_mtn" | python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:8])')"
}

pin_mcp_tools() {
  _pmt_profile="$1"
  _pmt_key="$2"
  shift 2
  [ "$#" -gt 0 ] || return 0
  _pmt_args=""
  for _pmt_t in "$@"; do
    _pmt_args="${_pmt_args} ${_pmt_t} $(mcp_tool_name "$_pmt_key" "$_pmt_t")"
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

# Set parked_retry_interval on every MCP entry that points at a stack sidecar,
# a client's entry on the same URL included (Boundary's calendar-<slug> on
# caldav-mcp, which addon_mcp_urls lists). Entries for other URLs, such as
# Boundary's own server, are left alone.
#
# Usage: set_stack_mcp_parked_retry <profile>
set_stack_mcp_parked_retry() {
  [ -n "$MCP_PARKED_RETRY_INTERVAL" ] || return 0
  _cfg="$(config_path_for "$1")"
  [ -f "$_cfg" ] || return 0
  # One argument per add-on URL.
  # shellcheck disable=SC2046
  _out="$(python3 - "$_cfg" "$MCP_PARKED_RETRY_INTERVAL" "$SEARXNG_MCP_URL" \
    "$OPENCODE_MCP_URL" \
    "$MAIL_MCP_URL" "$MAIL_DRAFTS_MCP_URL" "$MESSAGES_MCP_URL" \
    $(addon_mcp_urls) $(addon_instance_urls "$1") <<'PY'
import sys
sys.path.insert(0, "/opt/hermes")
try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import hermes_yaml as yaml
from utils import atomic_yaml_write

cfg_path, raw = sys.argv[1:3]
urls = set(sys.argv[3:])
interval = int(raw) if raw.isdigit() else raw
with open(cfg_path) as fh:
    config = yaml.safe_load(fh) or {}
servers = config.get("mcp_servers")
if not isinstance(servers, dict):
    raise SystemExit(0)
changed = [name for name, entry in servers.items()
           if isinstance(entry, dict) and entry.get("url") in urls
           and entry.get("parked_retry_interval") != interval]
for name in changed:
    servers[name]["parked_retry_interval"] = interval
if changed:
    atomic_yaml_write(cfg_path, config)
    print(f"Set parked_retry_interval={interval} on {', '.join(sorted(changed))}")
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
  case "$MESSAGES_MCP_ENABLED" in
    1|true|TRUE|yes|YES)
      if [ "$_rst_profile" != "api-server" ]; then
        for _rst_t in $MESSAGES_MCP_TOOLS; do
          _rst_args="${_rst_args} ${_rst_t} mcp__messages__${_rst_t}"
        done
      fi
      ;;
  esac
  _rst_args="${_rst_args} $(addon_mcp_pin_args "$_rst_profile")"
  _rst_args="${_rst_args} $(addon_instance_pin_args "$_rst_profile")"
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
# <keep-key>. A tool filter on the other entry does not save it: for the SQL
# endpoints the boundary is the database role behind the URL, so a filtered
# entry pointing at the read/write sidecar is still read/write.
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

# Built-in add-ons with an mcp block, from addons/*/addon.json
# (scripts/lib/addon_manifest.py mcp-plan). Each line of the plan says whether
# the add-on's flag is on and what access this profile gets; a profile the
# manifest leaves out gets no entry, which is how GPT Researcher stays off
# api-server. Invalid manifests are skipped with a warning on stderr.
# The mcp-plan lines for <profile>, or nothing when there are no manifests.
addon_mcp_plan() {
  [ -d "$ADDONS_DIR" ] || return 0
  python3 "$ADDON_MANIFEST_PY" mcp-plan "$ADDONS_DIR" "${1:-default}"
}

# Every add-on MCP URL, on or off, on any profile (profiles are ignored), one
# per line, for set_stack_mcp_parked_retry. That only tunes
# entries that already exist, so it also reaches a user's own entry on default.
addon_mcp_urls() {
  [ -d "$ADDONS_DIR" ] || return 0
  python3 "$ADDON_MANIFEST_PY" mcp-urls "$ADDONS_DIR" 2>/dev/null || true
}

# "<tool> <mcp__name__tool> ..." for each add-on that registers on <profile>,
# for reconcile_stack_eager_tools.
addon_mcp_pin_args() {
  addon_mcp_plan "$1" 2>/dev/null | while IFS='|' read -r _amp_name _amp_title _amp_on _amp_access \
      _amp_url _amp_timeout _amp_connect _amp_transport _amp_tools _amp_pins _amp_all_urls \
      _amp_keysrc _amp_rest; do
    { [ "$_amp_on" = 1 ] && [ -n "$_amp_access" ]; } || continue
    if [ -n "$_amp_keysrc" ]; then
      eval "_amp_key=\${${_amp_keysrc}:-}"
      [ -n "$_amp_key" ] || continue
      _amp_key=""
    fi
    for _amp_t in $_amp_pins; do
      printf ' %s %s' "$_amp_t" "$(mcp_tool_name "$_amp_name" "$_amp_t")"
    done
  done
  return 0
}

register_addon_mcp_servers() {
  _ram_profile="$1"
  [ -d "$ADDONS_DIR" ] || return 0
  if ! _ram_plan="$(addon_mcp_plan "$_ram_profile")"; then
    log "warning: could not read add-on manifests in ${ADDONS_DIR}; add-on MCP entries left as they were"
    return 0
  fi
  [ -n "$_ram_plan" ] || return 0
  printf '%s\n' "$_ram_plan" | while IFS='|' read -r _ram_name _ram_title _ram_on _ram_access \
      _ram_url _ram_timeout _ram_connect _ram_transport _ram_tools _ram_pins _ram_all_urls \
      _ram_keysrc _ram_keyenv _ram_header _ram_scheme; do
    _ram_skip=""
    if [ "$_ram_on" != 1 ]; then
      _ram_skip="Skipping ${_ram_title} MCP registration (add-on flag is off)"
    elif [ -z "$_ram_access" ]; then
      _ram_skip="Skipping ${_ram_title} MCP on '${_ram_profile:-default}' (not in the manifest's mcp.profiles)"
    elif [ -n "$_ram_keysrc" ]; then
      # mcp.auth: the token is in the variable the plan names (validated as
      # [A-Z][A-Z0-9_]*, so the eval only reads it).
      eval "_ram_key=\${${_ram_keysrc}:-}"
      [ -n "$_ram_key" ] \
        || _ram_skip="WARNING: ${_ram_title} is on but ${_ram_keysrc} is empty; no '${_ram_name}' on '${_ram_profile:-default}'"
    fi
    if [ -n "$_ram_skip" ]; then
      log "$_ram_skip"
      for _ram_u in $_ram_all_urls; do
        drop_mcp_entries_for_url "$_ram_profile" "" "$_ram_u"
      done
      if [ -n "$_ram_keyenv" ]; then
        remove_env_key "$(profile_env_file "$_ram_profile")" "$_ram_keyenv"
      fi
      continue
    fi
    # An add-on with a service per access level has a second URL, which this
    # profile must not keep.
    for _ram_u in $_ram_all_urls; do
      [ "$_ram_u" = "$_ram_url" ] || drop_mcp_entries_for_url "$_ram_profile" "" "$_ram_u"
    done
    if [ -n "$_ram_keysrc" ]; then
      # Like an instance with auth: the token goes into the profile's .env
      # and config.yaml holds only the header that references it. No
      # tools.include: the token's scopes decide which tools the server lists.
      _ram_env="$(profile_env_file "$_ram_profile")"
      mkdir -p "$(dirname "$_ram_env")"
      upsert_env "$_ram_env" "$_ram_keyenv" "$_ram_key"
      _ram_key=""
      log "Registering ${_ram_title} MCP server '${_ram_name}' for profile '${_ram_profile:-default}' at ${_ram_url}"
      write_bearer_mcp_entry "$_ram_profile" "$_ram_name" "$_ram_url" "$_ram_keyenv" \
        "$_ram_timeout" "$_ram_connect" "$_ram_header" "$_ram_scheme"
    else
      # shellcheck disable=SC2086
      register_mcp_server "$_ram_profile" "$_ram_name" "$_ram_title" \
        "$_ram_url" "$_ram_timeout" "$_ram_connect" $_ram_tools
    fi
    # A profile cloned from default can carry another key at this URL, with
    # its own (unfiltered) tool list.
    drop_mcp_entries_for_url "$_ram_profile" "$_ram_name" "$_ram_url"
    if [ "$_ram_transport" = sse ]; then
      hermes_config_set "$_ram_profile" "mcp_servers.${_ram_name}.transport" "sse"
    fi
    # shellcheck disable=SC2086
    pin_mcp_tools "$_ram_profile" "$_ram_name" $_ram_pins
  done
}

# Built-in add-ons with a vaultBackend block (addon_manifest.py vault-plan): a
# login-backend plugin for Hermes's browser vault (Bitwarden's bwbroker). The
# hermes container always installs the plugin into Hermes's bundled plugins
# dir (14-install-bwbroker-plugin.sh), since Hermes
# refuses a login backend from $HERMES_HOME/plugins. This turns it on or off
# per profile.
#
# While the add-on's flag is on and the manifest lists the profile, the
# plugin's name goes into plugins.enabled and vault.<name>.enabled is true.
# Otherwise both are removed. A copy of the plugin in the profile's own
# plugins dir is removed either way: it would shadow the bundled one and have
# its backend refused. While any vault add-on is on, vault.bitwarden.enabled
# is false, so core's own Bitwarden backend (the bw CLI) never lists logins
# beside the broker's. The config is written only when something changes, and
# then .addon-plugins-changed is left in the profile's home for
# scripts/vault.sh reload-plugins.
addon_vault_plan() {
  [ -d "$ADDONS_DIR" ] || return 0
  python3 "$ADDON_MANIFEST_PY" vault-plan "$ADDONS_DIR" "${1:-default}"
}

register_addon_vault_backends() {
  _rav_cfg="$(config_path_for "$1")"
  [ -f "$_rav_cfg" ] || return 0
  [ -d "$ADDONS_DIR" ] || return 0
  if ! _rav_plan="$(addon_vault_plan "$1")"; then
    log "warning: could not read add-on manifests in ${ADDONS_DIR}; add-on login backends left as they were"
    return 0
  fi
  [ -n "$_rav_plan" ] || return 0
  _out="$(python3 - "$_rav_cfg" "$_rav_plan" <<'PY'
import shutil
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
plan = [line.split("|") for line in sys.argv[2].splitlines() if line.count("|") == 4]
profile = cfg_path.parent.name if cfg_path.parent.parent.name == "profiles" else "default"
config = yaml.safe_load(cfg_path.read_text()) or {}
changes = []  # config edits
removed = []  # stale plugin copies


def section(parent, key, create):
    """parent[key] as a dict: created when missing and create is set; None when it isn't a dict."""
    value = parent.get(key)
    if value is None and create:
        value = parent[key] = {}
    return value if isinstance(value, dict) else None


def drop_if_empty(parent, key):
    if parent.get(key) in ({}, []):
        del parent[key]


for name, on, access, _plugin, _tools in plan:
    stale = cfg_path.parent / "plugins" / name
    if stale.is_symlink() or stale.is_file():
        stale.unlink()
        removed.append(str(stale))
    elif stale.is_dir():
        shutil.rmtree(stale)
        removed.append(str(stale))
    plugins = section(config, "plugins", on == "1" and bool(access))
    vault = section(config, "vault", on == "1" and bool(access))
    if on == "1" and access:
        if plugins is None or vault is None:
            print(f"WARNING: plugins or vault in {cfg_path} is not a mapping; {name} not enabled on {profile}")
            continue
        enabled = plugins.get("enabled")
        if enabled is None:
            plugins["enabled"] = [name]
            changes.append(f"enabled plugin {name}")
        elif not isinstance(enabled, list):
            print(f"WARNING: plugins.enabled on {profile} is not a list; {name} not enabled")
        elif name not in enabled:
            enabled.append(name)
            changes.append(f"enabled plugin {name}")
        disabled = plugins.get("disabled")
        if isinstance(disabled, list) and name in disabled:
            print(f"WARNING: plugins.disabled on {profile} lists {name}, so it stays off")
        backend = section(vault, name, True)
        if backend is None:
            print(f"WARNING: vault.{name} on {profile} is not a mapping; replaced with enabled: true")
            vault[name] = backend = {}
        if backend.get("enabled") is not True:
            backend["enabled"] = True
            changes.append(f"vault.{name}.enabled=true")
        continue
    if plugins is not None and isinstance(plugins.get("enabled"), list) and name in plugins["enabled"]:
        plugins["enabled"] = [p for p in plugins["enabled"] if p != name]
        drop_if_empty(plugins, "enabled")
        drop_if_empty(config, "plugins")
        changes.append(f"took plugin {name} out of plugins.enabled")
    if vault is not None and name in vault:
        del vault[name]
        drop_if_empty(config, "vault")
        changes.append(f"removed vault.{name}")

if any(on == "1" for _n, on, *_rest in plan):
    vault = section(config, "vault", True)
    if vault is None:
        print(f"WARNING: vault in {cfg_path} is not a mapping; vault.bitwarden left as it was")
    else:
        bitwarden = section(vault, "bitwarden", True)
        if bitwarden is None:
            print(f"WARNING: vault.bitwarden on {profile} is not a mapping; replaced with enabled: false")
            vault["bitwarden"] = bitwarden = {}
        if bitwarden.get("enabled") is not False:
            bitwarden["enabled"] = False
            changes.append("vault.bitwarden.enabled=false")

if changes:
    atomic_yaml_write(str(cfg_path), config)
    # A running gateway loaded its plugins at start; scripts/vault.sh up-hook
    # asks it to reload them for each profile that has this marker.
    (cfg_path.parent / ".addon-plugins-changed").touch()
if changes or removed:
    print(f"add-on login backends on {profile}: {'; '.join(changes + ['removed ' + r for r in removed])}")
PY
)"
  if [ -n "$_out" ]; then log "$_out"; fi
}

# Built-in add-ons with mcp.instances (Twenty CRM): one MCP entry per instance
# in the add-on's store, ADDON_DATA_DIR/<short id>/ (instances/<slug>.json and
# secrets/<slug>.<key setting>, mounted read-only). addon_manifest.py
# instances-plan decides, per profile, which instance registers and with which
# key file: a profile that mcp.profiles maps to ro (api-server) gets the ro
# key, one mapped to rw follows the instance's access (read -> ro, write -> rw).
# An entry with auth has its key copied into the profile's .env as <NAME>_KEY
# (crm-sales-eu -> CRM_SALES_EU_KEY), and config.yaml holds only the header
# that references it. The plan carries the key file's path, never the key.
# There is no tools.include: for Twenty, roles on the API key are the boundary
# (docs/crm.md). An entry without auth (Actual's budgets) has no key: it is
# registered with the plan's tool list, which leaves out write tools at ro.
addon_instances_plan() {
  [ -d "$ADDONS_DIR" ] || return 0
  python3 "$ADDON_MANIFEST_PY" instances-plan "$ADDONS_DIR" "$ADDON_DATA_DIR" "${1:-default}"
}

# crm-sales-eu -> CRM_SALES_EU_KEY (addon_manifest.instance_key_env).
addon_instance_key_env() {
  printf '%s_KEY' "$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')"
}

# Every instance URL in the plan, one per line, for set_stack_mcp_parked_retry.
addon_instance_urls() {
  addon_instances_plan "$1" 2>/dev/null | cut -d'|' -f4 | sed '/^$/d' || true
}

# "<tool> <mcp__name__tool> ..." for each instance that registers on <profile>,
# for reconcile_stack_eager_tools.
addon_instance_pin_args() {
  addon_instances_plan "$1" 2>/dev/null | while IFS='|' read -r _aip_name _aip_title _aip_on _aip_url \
      _aip_keyenv _aip_keyfile _aip_timeout _aip_connect _aip_pins _aip_rest; do
    [ "$_aip_on" = 1 ] || continue
    case "$_aip_name" in *'*') continue ;; esac
    for _aip_t in $_aip_pins; do
      printf ' %s %s' "$_aip_t" "$(mcp_tool_name "$_aip_name" "$_aip_t")"
    done
  done
  return 0
}

# Write mcp_servers.<key> with a header that references the profile's env key,
# "<scheme> ${<env-key>}". hermes config set cannot write a header map.
#
# Usage: write_bearer_mcp_entry <profile> <key> <url> <env-key> <timeout> <connect-timeout> [header] [scheme]
write_bearer_mcp_entry() {
  _out="$(python3 - "$(config_path_for "$1")" "$2" "$3" "$4" "$5" "$6" \
    "${7:-Authorization}" "${8:-Bearer}" <<'PY'
import sys
sys.path.insert(0, "/opt/hermes")
try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import hermes_yaml as yaml
from utils import atomic_yaml_write

cfg_path, key, url, env_key, timeout, connect_timeout, header, scheme = sys.argv[1:9]
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
headers[header] = scheme + " ${" + env_key + "}"
entry["headers"] = headers
servers[key] = entry
atomic_yaml_write(cfg_path, config)
print(f"Wrote mcp_servers.{key}")
PY
)"
  if [ -n "$_out" ]; then log "$_out"; fi
}

# Write mcp_servers.<key> with the headers an mcp.instances.headers block
# filled in (a JSON object; a secret is already a ${<env-key>} reference),
# replacing any headers the entry had, and the tool allowlist. hermes config
# set cannot write a header map.
#
# Usage: write_headers_mcp_entry <profile> <key> <url> <timeout> <connect-timeout> <headers-json> [tool...]
write_headers_mcp_entry() {
  _whm_cfg="$(config_path_for "$1")"
  _whm_key="$2"
  _whm_url="$3"
  _whm_timeout="$4"
  _whm_connect="$5"
  _whm_headers="$6"
  shift 6
  _out="$(python3 - "$_whm_cfg" "$_whm_key" "$_whm_url" "$_whm_timeout" "$_whm_connect" \
    "$_whm_headers" "$@" <<'PY'
import json
import sys
sys.path.insert(0, "/opt/hermes")
try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import hermes_yaml as yaml
from utils import atomic_yaml_write

cfg_path, key, url, timeout, connect_timeout, headers = sys.argv[1:7]
tools = sys.argv[7:]
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
entry["headers"] = json.loads(headers)
if tools:
    tools_block = entry.get("tools")
    if not isinstance(tools_block, dict):
        tools_block = {}
    tools_block["include"] = tools
    entry["tools"] = tools_block
servers[key] = entry
atomic_yaml_write(cfg_path, config)
print(f"Wrote mcp_servers.{key} ({len(tools)} tools)" if tools else f"Wrote mcp_servers.{key}")
PY
)"
  if [ -n "$_out" ]; then log "$_out"; fi
}

# Drop entries whose name starts with an add-on's instance prefix (crm-) and
# isn't in the keep list: an instance that is gone or no longer registers on
# this profile. Their key (<NAME>_KEY, or <NAME>_<SUFFIX> for a prefix listed
# in _rai_keysfx as <prefix>=<SUFFIX>) and tool pins go with them, and with no entry
# left under a prefix, that add-on's bare tool names leave the pins too. Only
# entries under the prefixes are ours; entries a client added under another
# name stay, even at the same URL.
#
# Twenty only: before 0.3.13 a workspace's key was TWENTY_<SLUG>_KEY; that
# line is removed for every crm-* entry in the config, kept or dropped.
#
# Usage: reconcile_addon_instance_servers <profile> [<prefix>=<pin,pin...>]... [<name>]...
reconcile_addon_instance_servers() {
  _rais_profile="$1"
  shift
  _out="$(python3 - "$(config_path_for "$_rais_profile")" "$@" <<'PY'
import re
import sys
sys.path.insert(0, "/opt/hermes")
try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import hermes_yaml as yaml
from utils import atomic_yaml_write

cfg_path = sys.argv[1]
bare, keep = {}, set()
for arg in sys.argv[2:]:
    if "=" in arg:
        prefix, pins = arg.split("=", 1)
        bare[prefix] = {p for p in pins.split(",") if p}
    else:
        keep.add(arg)
if not bare:
    raise SystemExit(0)
try:
    with open(cfg_path) as fh:
        config = yaml.safe_load(fh) or {}
except FileNotFoundError:
    raise SystemExit(0)
servers = config.get("mcp_servers")
if not isinstance(servers, dict):
    servers = {}
prefixes = tuple(bare)
ours = sorted(k for k in servers if isinstance(k, str) and k.startswith(prefixes))
removed = [k for k in ours if k not in keep]
for key in removed:
    del servers[key]


def sanitize(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


# Pins of servers that are gone: mcp__crm_<slug>__* (Hermes' tool names) and
# the mcp__crm-<slug>__* form earlier builds wrote, which never matched.
kept_prefixes = tuple(p for s in keep for p in (f"mcp__{sanitize(s)}__", f"mcp__{s}__"))
unpinned = 0
tool_search = ((config.get("tools") or {}).get("tool_search") or {})
pins = tool_search.get("always_include")
if isinstance(pins, list):
    left = {p: any(k.startswith(p) for k in servers) for p in prefixes}

    def stale(name):
        if not isinstance(name, str):
            return False
        for p in prefixes:
            if name.startswith((f"mcp__{sanitize(p)}", f"mcp__{p}")):
                return not name.startswith(kept_prefixes)
        return any(not left[p] and name in bare[p] for p in prefixes)

    kept_pins = [p for p in pins if not stale(p)]
    unpinned = len(pins) - len(kept_pins)
    tool_search["always_include"] = kept_pins

if removed or unpinned:
    atomic_yaml_write(cfg_path, config)
if unpinned:
    print(f"Removed {unpinned} stale add-on instance tool pins")
if removed:
    print("Removed stale add-on instance MCP entries: " + ", ".join(removed))
for key in removed:
    print("REMOVED " + key)
for key in ours:
    print("SEEN " + key)
PY
)"
  [ -n "$_out" ] || return 0
  _rais_env="$(profile_env_file "$_rais_profile")"
  printf '%s\n' "$_out" | while IFS= read -r _rais_line; do
    case "$_rais_line" in
      REMOVED\ *)
        _rais_name="${_rais_line#REMOVED }"
        remove_env_key "$_rais_env" "$(addon_instance_key_env "$_rais_name")"
        for _rais_sfx in ${_rai_keysfx:-}; do
          case "$_rais_name" in
            "${_rais_sfx%%=*}"*)
              remove_env_key "$_rais_env" \
                "$(printf '%s_%s' "$_rais_name" "${_rais_sfx#*=}" | tr '[:lower:]-' '[:upper:]_')"
              ;;
          esac
        done
        ;;
      SEEN\ crm-*)
        # Twenty only, for the 0.3.13 upgrade: a crm-* entry still in the
        # config may have its key under the old name. A legacy key whose
        # entry was already gone stays; there is no wider sweep.
        _rais_legacy="TWENTY_$(printf '%s' "${_rais_line#SEEN crm-}" | tr '[:lower:]-' '[:upper:]_')_KEY"
        if grep -q "^${_rais_legacy}=" "$_rais_env" 2>/dev/null; then
          remove_env_key "$_rais_env" "$_rais_legacy"
        fi
        ;;
      SEEN\ *) ;;
      *) log "$_rais_line" ;;
    esac
  done
}

# After the last instance line of an add-on: warn when its flag is on and no
# instance registered, unless there were instances and every one was skipped
# for an expected reason: a gate (the local Twenty instance is off), a profile
# the manifest leaves out, or no ro key on api-server.
addon_instances_warn_none() {
  [ -n "$_rai_title" ] || return 0
  [ "$_rai_addon_on" = 1 ] && [ "$_rai_count" = 0 ] || return 0
  [ "$_rai_seen" = 0 ] || [ "$_rai_quiet" != "$_rai_seen" ] || return 0
  log "WARNING: ${_rai_title} is on but no instance registers on '${_rai_profile:-default}' - connect one, or add the key this profile needs"
}

register_addon_instance_servers() {
  _rai_profile="$1"
  [ -d "$ADDONS_DIR" ] || return 0
  if ! _rai_plan="$(addon_instances_plan "$_rai_profile")"; then
    log "warning: could not read add-on instances in ${ADDON_DATA_DIR}; their MCP entries left as they were"
    return 0
  fi
  [ -n "$_rai_plan" ] || return 0
  _rai_env="$(profile_env_file "$_rai_profile")"
  _rai_specs=""
  _rai_keysfx=""
  _rai_keep=""
  _rai_title=""
  while IFS='|' read -r _rai_name _rai_t _rai_on _rai_url _rai_keyenv _rai_keyfile \
      _rai_timeout _rai_connect _rai_pins _rai_reason _rai_header _rai_scheme _rai_tools \
      _rai_headers; do
    case "$_rai_name" in
      '') continue ;;
      *'*')
        # The add-on's own line: its prefix, flag and bare pin names.
        addon_instances_warn_none
        _rai_title="$_rai_t"
        _rai_addon_on="$_rai_on"
        _rai_count=0
        _rai_seen=0
        _rai_quiet=0
        _rai_specs="${_rai_specs} ${_rai_name%\*}=$(printf '%s' "$_rai_pins" | tr ' ' ',')"
        # The add-on line's keyenv is its key suffix (PASSWORD for CalDAV).
        case "$_rai_keyenv" in
          ''|KEY) ;;
          *) _rai_keysfx="${_rai_keysfx} ${_rai_name%\*}=${_rai_keyenv}" ;;
        esac
        if [ "$_rai_on" != 1 ]; then
          case "$_rai_reason" in
            *' '*) log "Skipping ${_rai_t} MCP registration (${_rai_reason})" ;;
            *) log "Skipping ${_rai_t} MCP registration (${_rai_reason} is off)" ;;
          esac
        fi
        continue
        ;;
    esac
    _rai_seen=$((_rai_seen + 1))
    if [ "$_rai_on" != 1 ]; then
      case "$_rai_reason" in
        flag\ *) ;;
        gate\ *|profile\ *)
          log "Skipping ${_rai_t} instance '${_rai_name}' on '${_rai_profile:-default}' (${_rai_reason#* })"
          _rai_quiet=$((_rai_quiet + 1))
          ;;
        key\ *)
          if [ "$_rai_profile" = "api-server" ]; then
            log "Skipping ${_rai_t} instance '${_rai_name}' on api-server (${_rai_reason#key })"
            _rai_quiet=$((_rai_quiet + 1))
          else
            log "WARNING: Skipping ${_rai_t} instance '${_rai_name}' - ${_rai_reason#key }"
          fi
          ;;
        *) log "WARNING: Skipping ${_rai_t} instance '${_rai_name}' - ${_rai_reason}" ;;
      esac
      continue
    fi
    if [ -n "$_rai_headers" ]; then
      # mcp.instances.headers (CalDAV): the account rides in the headers, and
      # its secret, if any, in the profile's .env.
      if [ -n "$_rai_keyfile" ]; then
        _rai_key="$(cat "$_rai_keyfile" 2>/dev/null)" || _rai_key=""
        if [ -z "$_rai_key" ]; then
          log "WARNING: Skipping ${_rai_t} instance '${_rai_name}' - cannot read its key file"
          continue
        fi
        mkdir -p "$(dirname "$_rai_env")"
        upsert_env "$_rai_env" "$_rai_keyenv" "$_rai_key"
        _rai_key=""
      fi
      log "Registering ${_rai_t} MCP server '${_rai_name}' for profile '${_rai_profile:-default}' at ${_rai_url}"
      # shellcheck disable=SC2086
      write_headers_mcp_entry "$_rai_profile" "$_rai_name" "$_rai_url" "$_rai_timeout" \
        "$_rai_connect" "$_rai_headers" $_rai_tools
    elif [ -z "$_rai_keyfile" ]; then
      # shellcheck disable=SC2086
      register_mcp_server "$_rai_profile" "$_rai_name" "$_rai_t" \
        "$_rai_url" "$_rai_timeout" "$_rai_connect" $_rai_tools
    else
      _rai_key="$(cat "$_rai_keyfile" 2>/dev/null)" || _rai_key=""
      if [ -z "$_rai_key" ]; then
        log "WARNING: Skipping ${_rai_t} instance '${_rai_name}' - cannot read its key file"
        continue
      fi
      mkdir -p "$(dirname "$_rai_env")"
      upsert_env "$_rai_env" "$_rai_keyenv" "$_rai_key"
      _rai_key=""
      log "Registering ${_rai_t} MCP server '${_rai_name}' for profile '${_rai_profile:-default}' at ${_rai_url}"
      write_bearer_mcp_entry "$_rai_profile" "$_rai_name" "$_rai_url" "$_rai_keyenv" \
        "$_rai_timeout" "$_rai_connect" "$_rai_header" "$_rai_scheme"
    fi
    # shellcheck disable=SC2086
    pin_mcp_tools "$_rai_profile" "$_rai_name" $_rai_pins
    _rai_keep="${_rai_keep} ${_rai_name}"
    _rai_count=$((_rai_count + 1))
  done <<EOF
$_rai_plan
EOF
  addon_instances_warn_none
  # shellcheck disable=SC2086
  reconcile_addon_instance_servers "$_rai_profile" $_rai_specs $_rai_keep
}

# Read-only Messages index (messages compose profile), registered as
# mcp_servers.messages on the interactive profiles only, like mail: a stranger
# can text you, so unattended api-server sessions never get it. The default
# profile also gets messages_triage_batch for the messages-triage cron job.
register_messages_mcp_servers() {
  _rmsg_profile="$1"
  case "$MESSAGES_MCP_ENABLED" in
    1|true|TRUE|yes|YES) ;;
    *)
      log "Skipping messages MCP registration (MESSAGES_MCP_ENABLED=${MESSAGES_MCP_ENABLED})"
      drop_mcp_entries_for_url "$_rmsg_profile" "" "$MESSAGES_MCP_URL"
      return 0
      ;;
  esac
  if [ "$_rmsg_profile" = "api-server" ]; then
    log "Skipping messages MCP on api-server profile (unattended sessions)"
    drop_mcp_entries_for_url "$_rmsg_profile" "" "$MESSAGES_MCP_URL"
    return 0
  fi
  _rmsg_tools="$MESSAGES_MCP_TOOLS"
  if [ -z "$_rmsg_profile" ]; then
    _rmsg_tools="${_rmsg_tools} ${MESSAGES_TRIAGE_TOOL}"
  fi
  # shellcheck disable=SC2086
  register_mcp_server "$_rmsg_profile" messages "Messages (read-only)" \
    "$MESSAGES_MCP_URL" "$MESSAGES_MCP_TIMEOUT" "$MESSAGES_MCP_CONNECT_TIMEOUT" $_rmsg_tools
  # shellcheck disable=SC2086
  pin_mcp_tools "$_rmsg_profile" messages $MESSAGES_MCP_TOOLS
}

# New-message events reach Hermes as signed POSTs from messages-mcp's
# forwarder to a webhook route on the default profile, which fires the
# messages-triage cron job (make messages-triage-cron). The route's prompt
# carries only the message count: chat and sender names can be set by other
# people. The listener stays inside the compose network; no port is published.
#
# Only the `messages` route is ours. Routes you added are kept. If we turned
# the webhook platform on, that's recorded in .messages-webhook.json beside
# config.yaml, and it's turned off again when the route goes and nothing else
# uses it.
configure_messages_webhook() {
  _cmw_cfg="$(config_path_for "")"
  [ -f "$_cmw_cfg" ] || return 0
  _cmw_on=0
  case "$MESSAGES_MCP_ENABLED" in
    1|true|TRUE|yes|YES) [ -n "$MESSAGES_WEBHOOK_SECRET" ] && _cmw_on=1 ;;
  esac
  _out="$(MESSAGES_WEBHOOK_SECRET="$MESSAGES_WEBHOOK_SECRET" python3 - "$_cmw_cfg" "$_cmw_on" "$MESSAGES_WEBHOOK_PORT" <<'PY'
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/opt/hermes")
try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import hermes_yaml as yaml
from utils import atomic_yaml_write

cfg_path, on, port = Path(sys.argv[1]), sys.argv[2] == "1", int(sys.argv[3])
state_path = cfg_path.parent / ".messages-webhook.json"
PROMPT = ("{count} new iMessage/SMS message(s) arrived in a chat you watch. Call messages_triage_batch "
          "once and report as your instructions say.")

config = yaml.safe_load(cfg_path.read_text()) or {}
platforms = config.get("platforms") if isinstance(config.get("platforms"), dict) else {}
webhook = platforms.get("webhook") if isinstance(platforms.get("webhook"), dict) else {}
extra = webhook.get("extra") if isinstance(webhook.get("extra"), dict) else {}
routes = extra.get("routes") if isinstance(extra.get("routes"), dict) else {}
try:
    state = json.loads(state_path.read_text())
except (OSError, ValueError):
    state = {}

if on:
    route = {"secret": os.environ["MESSAGES_WEBHOOK_SECRET"], "events": ["messages.new"],
             "cron_job": "messages-triage", "prompt": PROMPT}
    changed = routes.get("messages") != route
    routes["messages"] = route
    if webhook.get("enabled") is not True:
        webhook["enabled"] = True
        state["enabled_platform"] = True
        changed = True
    if "port" not in extra and "port" not in webhook:
        extra["port"] = port
        extra.setdefault("host", "0.0.0.0")
        changed = True
    elif int(extra.get("port", webhook.get("port"))) != port:
        print(f"WARNING: platforms.webhook already uses port {extra.get('port', webhook.get('port'))}; "
              f"set MESSAGES_WEBHOOK_PORT to match so messages-mcp can reach it")
    if not changed:
        print("messages webhook route already set")
        raise SystemExit(0)
    extra["routes"] = routes
    webhook["extra"] = extra
    platforms["webhook"] = webhook
    config["platforms"] = platforms
    atomic_yaml_write(str(cfg_path), config)
    state_path.write_text(json.dumps(state))
    print(f"set the messages webhook route (port {extra.get('port', webhook.get('port'))}, fires messages-triage)")
else:
    if "messages" not in routes and not state:
        raise SystemExit(0)
    routes.pop("messages", None)
    if state.get("enabled_platform") and not routes:
        platforms.pop("webhook", None)
    else:
        extra["routes"] = routes
        webhook["extra"] = extra
        platforms["webhook"] = webhook
    config["platforms"] = platforms
    atomic_yaml_write(str(cfg_path), config)
    state_path.unlink(missing_ok=True)
    print("removed the messages webhook route")
PY
)"
  if [ -n "$_out" ]; then log "$_out"; fi
}

# Keep the mail and messages tools, and every add-on whose manifest doesn't
# set permissions.cron true, away from cron jobs that didn't ask for them.
#
# Cron runs unattended, and a job without its own tool list gets every MCP
# server, mail and messages included, next to web search, the browser and a
# shell. While one of the fenced servers is registered on a profile, pin
# platform_toolsets.cron to Hermes's default cron tools plus every other MCP
# server. A job that names a fenced server in its own enabled_toolsets (make
# mail-triage-cron, make messages-triage-cron) still gets it. Add-ons are
# matched by MCP name, or by the scheme, host and port of a URL
# (addon_manifest.cron_fence), so a connected Actual budget's write entry on
# actual-mcp-rw is fenced while its -ro entry on actual-mcp-ro is not.
#
# Add-ons with a vaultBackend reach Hermes through a plugin, not MCP, and the
# plugin's toolset (bitwarden) is on in cron by default. While the plugin is
# enabled on a profile, the toolset is listed in known_plugin_toolsets.cron,
# which turns it off there, and taken out of platform_toolsets.cron
# (addon_manifest.cron_fence_toolsets). A job naming it in its own
# enabled_toolsets still gets the tools; the backend lists no logins in a
# cron turn either way.
#
# A list we wrote is recorded in .mail-cron-toolsets.json next to config.yaml,
# so it can be refreshed or removed later. A list you wrote yourself is kept;
# only the fenced servers are taken out of it.
fence_mail_from_cron() {
  _fmc_cfg="$(config_path_for "$1")"
  [ -f "$_fmc_cfg" ] || return 0
  _out="$(python3 - "$_fmc_cfg" "$ADDON_MANIFEST_PY" "$ADDONS_DIR" "$ADDON_DATA_DIR" <<'PY'
import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, "/opt/hermes")
try:
    import yaml
except ImportError:  # Hermes v0.22+ ships ruamel only; hermes_yaml wraps it
    import sys as _sys
    _sys.path.insert(0, "/opt/hermes")
    import hermes_yaml as yaml
from utils import atomic_yaml_write

MAIL_URLS = ("mail-mcp:8000", "mail-drafts-mcp:8000", "messages-mcp:8000")
cfg_path = Path(sys.argv[1])


def origin(url):
    """(scheme, host, port) of a URL, None when it has no host. A connected
    Actual budget's write entry is http://actual-mcp-rw:3000/<slug>/mcp, on
    the host of the fenced http://actual-mcp-rw:3000/mcp."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port or {"http": 80, "https": 443}.get(parts.scheme.lower())
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return (parts.scheme.lower(), parts.hostname.lower(), port)


addon_names, addon_urls, addon_toolsets = set(), set(), []
fence_known = True
try:
    spec = importlib.util.spec_from_file_location("addon_manifest", sys.argv[2])
    addon_manifest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon_manifest)
    _names, _urls = addon_manifest.cron_fence(Path(sys.argv[3]), data_dir=Path(sys.argv[4]))
    addon_names, addon_urls = set(_names), set(_urls)
    addon_toolsets = addon_manifest.cron_fence_toolsets(Path(sys.argv[3]))
except Exception as exc:  # no add-on is registered without the module either
    print(f"warning: add-on cron fence unavailable: {exc}")
    fence_known = False
state_path = cfg_path.parent / ".mail-cron-toolsets.json"
profile = cfg_path.parent.name if cfg_path.parent.parent.name == "profiles" else "default"

config = yaml.safe_load(cfg_path.read_text()) or {}
notes = []
dirty = False


def finish(after=None):
    if dirty:
        atomic_yaml_write(str(cfg_path), config)
    if after:
        after()
    for note in notes:
        print(note)
    raise SystemExit(0)


def names(value):
    return {str(v) for v in value} if isinstance(value, list) else set()


# Hermes turns a plugin's toolset on for every platform, cron included, unless
# known_plugin_toolsets.<platform> lists it and platform_toolsets.<platform>
# doesn't. A fenced add-on's toolset goes into known_plugin_toolsets.cron while
# its plugin is enabled on this profile, and comes out once it isn't.
plugins_cfg = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
on_plugins = names(plugins_cfg.get("enabled")) - names(plugins_cfg.get("disabled"))
plugin_ts = {ts for name, ts in addon_toolsets if name in on_plugins}
idle_ts = {ts for _name, ts in addon_toolsets} - plugin_ts
known = config.get("known_plugin_toolsets")
if plugin_ts and known is None:
    known = config["known_plugin_toolsets"] = {}
if isinstance(known, dict):
    known_cron = known.get("cron") if isinstance(known.get("cron"), list) else []
    want = [t for t in known_cron if t not in idle_ts] + sorted(plugin_ts - names(known_cron))
    if want != known_cron:
        if want:
            known["cron"] = want
        else:
            known.pop("cron", None)
        if not known:
            config.pop("known_plugin_toolsets", None)
        dirty = True
        notes.append(f"plugin toolsets cron jobs on {profile} don't get unless they ask: {', '.join(want) or 'none'}")
elif plugin_ts:
    notes.append(f"WARNING: known_plugin_toolsets on {profile} is not a mapping; "
                 f"{', '.join(sorted(plugin_ts))} not kept out of cron")

servers = config.get("mcp_servers") or {}
enabled = {str(n) for n, s in servers.items()
           if isinstance(s, dict) and str(s.get("enabled", True)).lower() not in ("false", "no", "off", "0")}
addon_origins = {o for o in map(origin, addon_urls) if o}
mail = {n for n in enabled
        if any(u in str(servers[n].get("url", "")) for u in MAIL_URLS)
        or n in addon_names or origin(str(servers[n].get("url", ""))) in addon_origins}
platform = config.get("platform_toolsets")
platform = platform if isinstance(platform, dict) else {}
current = platform.get("cron")
try:
    managed = json.loads(state_path.read_text()) == current
except (OSError, ValueError):
    managed = False
others = sorted(enabled - mail) or ["no_mcp"]
# A fenced plugin toolset named in a cron list would be on despite known_plugin_toolsets.
listed_ts = plugin_ts & names(current)

if not fence_known and managed and current is not None:
    # Without the manifests we can't tell which add-ons to fence; keep the
    # list we wrote last time rather than open cron to them.
    notes.append(f"kept platform_toolsets.cron on {profile} as it was")
    finish()

if not mail:
    if managed and current is not None:
        platform.pop("cron", None)
        config["platform_toolsets"] = platform
        dirty = True
        notes.append(f"removed the cron tool list added for fenced servers ({profile})")
        finish(lambda: state_path.unlink(missing_ok=True))
    if listed_ts:
        platform["cron"] = [t for t in current if str(t) not in listed_ts]
        config["platform_toolsets"] = platform
        dirty = True
        notes.append(f"took {', '.join(sorted(listed_ts))} out of your platform_toolsets.cron on {profile}")
    finish()

if current is None or managed:
    desired = ["hermes-cron"] + others
else:
    desired = [t for t in current if t not in mail and str(t) not in listed_ts]
    if not set(desired) & (enabled | {"no_mcp"}):
        # A list naming no MCP server gets all of them, mail included.
        desired += others
if desired == current:
    notes.append(f"cron jobs on {profile} already can't reach {', '.join(sorted(mail))} unless they ask")
    finish()
platform["cron"] = desired
config["platform_toolsets"] = platform
dirty = True
if current is None or managed:
    notes.append(f"pinned platform_toolsets.cron on {profile} without {', '.join(sorted(mail))}: {', '.join(desired)}")
    finish(lambda: state_path.write_text(json.dumps(desired)))
notes.append(f"took {', '.join(sorted(mail | listed_ts))} out of your platform_toolsets.cron on {profile}")
finish()
PY
)"
  if [ -n "$_out" ]; then log "$_out"; fi
}

# Default profile secrets live in HERMES_HOME/.env. Named profiles do not
# inherit that file, so each registration writes its own copy.
profile_env_file() {
  if [ -z "$1" ]; then
    printf '%s\n' "$DEFAULT_ENV"
  else
    printf '%s/profiles/%s/.env\n' "$HERMES_HOME" "$1"
  fi
}

if [ ! -f "${HERMES_HOME}/config.yaml" ]; then
  log "No ${HERMES_HOME}/config.yaml — run setup first:"
  log "  docker compose --profile hermes run --rm hermes setup"
  exit 0
fi

# --- Skip an unchanged run (see "Skipping an unchanged run" above) ---
if [ -f "$SOURCE_ENV" ]; then
  INPUTS_HASH="$(bootstrap_inputs_hash)"
  if is_true "$BOOTSTRAP_FORCE"; then
    log "HERMES_BOOTSTRAP_FORCE is set; running the full bootstrap"
  elif [ -d "$PROFILE_DIR" ] && [ "$INPUTS_HASH" = "$(cat "${STAMP_DIR}/${PROFILE}" 2>/dev/null)" ] \
    && [ "$(bootstrap_state_hash)" = "$(cat "${STAMP_DIR}/state" 2>/dev/null)" ]; then
    log "Inputs and config unchanged since the last run; skipping (HERMES_BOOTSTRAP_FORCE=1 runs it anyway)"
    mark_gateway_running
    exit 0
  fi
fi
# A run that stops part way must not leave the old stamp behind.
rm -f "${STAMP_DIR}/${PROFILE}"

if is_true "$BOOTSTRAP_SHARED"; then
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
  register_opencode_mcp_servers ""
  register_messages_mcp_servers ""
  register_addon_mcp_servers ""
  register_addon_vault_backends ""
  register_addon_instance_servers ""

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
fi

if [ ! -f "$SOURCE_ENV" ]; then
  env_name="$(basename "$SOURCE_ENV")"
  log "Missing ${SOURCE_ENV} — copy compose/hermes/${env_name}.example to compose/hermes/${env_name}"
  exit 1
fi

# --- Create profile if missing ---
# The clone copies the default profile's config, so apply its queued keys first.
config_flush
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

if is_true "$BOOTSTRAP_SHARED"; then
  # --- Default profile's API server, per gateway topology ---
  # Same resolution as compose/hermes/019-pin-profile-gateways.sh.
  GATEWAY_TOPOLOGY="$(printf '%s' "${HERMES_GATEWAY_TOPOLOGY:-}" | tr '[:upper:]' '[:lower:]')"
  if [ -z "$GATEWAY_TOPOLOGY" ]; then
    case "$(printf '%s' "${HERMES_PIN_STANDALONE_GATEWAYS:-1}" | tr '[:upper:]' '[:lower:]')" in
      0|false|no|off) GATEWAY_TOPOLOGY=multiplex ;;
      *) GATEWAY_TOPOLOGY=standalone ;;
    esac
  fi
  touch "$DEFAULT_ENV"
  if [ "$GATEWAY_TOPOLOGY" = multiplex ]; then
    # The default listener (8642) serves every profile at /p/<profile>/ and
    # needs a key of its own; 019-pin-profile-gateways generates it. The WebUI
    # keeps working because it prefers HERMES_WEBUI_GATEWAY_API_KEY over the
    # API_SERVER_KEY its _reload_dotenv reads from here.
    remove_env_key "$DEFAULT_ENV" "API_SERVER_ENABLED"
    upsert_env "$DEFAULT_ENV" "API_SERVER_HOST" "0.0.0.0"
  else
    upsert_env "$DEFAULT_ENV" "API_SERVER_ENABLED" "false"
    # Drop any leftover default-profile API_SERVER_KEY. Hermes WebUI loads
    # HERMES_HOME/.env via _reload_dotenv and overwrites container env; a stale
    # key here replaces the browser.env credential and every WebUI turn 401s.
    remove_env_key "$DEFAULT_ENV" "API_SERVER_KEY"
    remove_env_key "$DEFAULT_ENV" "API_SERVER_HOST"
  fi
  remove_env_key "$DEFAULT_ENV" "API_SERVER_PORT"
  remove_env_key "$DEFAULT_ENV" "HERMES_MAX_ITERATIONS"
fi

# --- Apply profile config ---
# hermes-api-server matches dashboard chat (hermes-cli) minus the interactive-only
# tools (clarify, send_message, text_to_speech), which avoids web-only forced
# search loops. Interactive profiles pass HERMES_API_TOOLSET=hermes-cli to get
# those back; clarify turns an ambiguous browser request into a question instead
# of a guess, and there is a human present to answer it.
# Since v0.22, api_server runs keep only the configurable toolsets when they
# expand hermes-cli, which drops send_message (patch-send-message-tool.py
# registers it under its own toolset). Name it explicitly so WebUI turns can
# still deliver to Signal.
log "Setting agent.max_turns=${MAX_TURNS} and ${API_TOOLSET} toolset for api_server platform"
hermes_config_set "$PROFILE" "agent.max_turns" "$MAX_TURNS"
set_yaml_list "$(config_path_for "$PROFILE")" "platform_toolsets.api_server" "$API_TOOLSET"
if [ "$API_TOOLSET" = hermes-cli ]; then
  ensure_yaml_list_items "$(config_path_for "$PROFILE")" "platform_toolsets.api_server" send_message
fi

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
log "Disabling session-only todo and native web toolsets on '${PROFILE}'$(is_true "$BOOTSTRAP_SHARED" && echo ' and default')"
if is_true "$BOOTSTRAP_SHARED"; then
  ensure_yaml_list_items "$(config_path_for "")" "agent.disabled_toolsets" "todo" "web"
fi
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

# Same web-reading servers as the default profile above; profiles carry their
# own config.yaml, so the earlier registration does not reach API sessions.
register_web_mcp_servers "$PROFILE"
register_opencode_mcp_servers "$PROFILE"
register_messages_mcp_servers "$PROFILE"
register_addon_mcp_servers "$PROFILE"
register_addon_vault_backends "$PROFILE"
register_addon_instance_servers "$PROFILE"

# Heal always_include + defer after every hermes config set / MCP register.
if is_true "$BOOTSTRAP_SHARED"; then
  reconcile_stack_eager_tools ""
  set_stack_mcp_parked_retry ""
fi
reconcile_stack_eager_tools "$PROFILE"
set_stack_mcp_parked_retry "$PROFILE"

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

if is_true "$BOOTSTRAP_SHARED"; then
  fence_mail_from_cron ""
  configure_messages_webhook
fi
fence_mail_from_cron "$PROFILE"

apply_terminal_backend_all_profiles
apply_model_proxy_all_profiles
apply_tool_loop_hard_stop_all_profiles
config_flush || exit 1

mark_gateway_running

# Record this run for the next start (see "Skipping an unchanged run" above).
if [ -n "${INPUTS_HASH:-}" ]; then
  mkdir -p "$STAMP_DIR"
  printf '%s\n' "$INPUTS_HASH" > "${STAMP_DIR}/${PROFILE}"
  bootstrap_state_hash > "${STAMP_DIR}/state"
fi

log "Done: profile=${PROFILE} port=${API_PORT} max_turns=${MAX_TURNS} toolset=${API_TOOLSET}"
log "API URL (host): http://localhost:${API_PORT}/v1"
log "API URL (compose): http://hermes:${API_PORT}/v1"
