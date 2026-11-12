#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# Which Hermes profiles hermes-*-bootstrap gives the mail MCP, and which tools.
# Registration goes through the generic add-on path (register_addon_mcp_servers
# reading addons/email-local/addon.json); the calls it makes must match what
# register_mail_mcp_servers made before the manifest replaced it.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
ADDONS_DIR="${ROOT}/addons"
ADDON_MANIFEST_PY="${ROOT}/scripts/lib/addon_manifest.py"
export STACK_PYTHON=host

load_fns "$bootstrap" addon_mcp_plan addon_mcp_pin_args register_addon_mcp_servers mcp_tool_name

log() { :; }
# The todos entry (mcp.auth) removes its profile key while Vikunja is off.
caldav_profile_env_file() { :; }
remove_env_key() { :; }

# The real addons/ dir: other add-ons add their own calls, so the stubs record
# only calls about the mail server. The plan is read in a pipeline, so the
# record comes back through a file rather than the subshell's $calls.
URL="http://mail-mcp:8000/mcp"
record="$(mktemp)"
trap 'rm -f "$record"' EXIT
run() {
  register_addon_mcp_servers "$1" > /dev/null
}
register_mcp_server() {
  [ "$2" = mail ] || return 0
  printf 'register:%s:%s:%s:%s:%s:%s:%s;' "${1:-default}" "$2" "$3" "$4" "$5" "$6" "$(shift 6; echo "$*")" >> "$record"
}
hermes_config_set() {
  case "$2" in mcp_servers.mail.*) printf 'set:%s:%s=%s;' "${1:-default}" "$2" "$3" >> "$record" ;; esac
}
pin_mcp_tools() {
  [ "$2" = mail ] || return 0
  printf 'pin:%s:%s;' "${1:-default}" "$(shift 2; echo "$*")" >> "$record"
}
drop_mcp_entries_for_url() {
  case "$3" in "$URL"|http://elsewhere:*) printf 'drop:%s:%s;' "${1:-default}" "$3" >> "$record" ;; esac
}
got() { calls="$(cat "$record")"; : > "$record"; }

TOOLS="mail_status mail_search mail_get mail_thread"

export MAIL_MCP_ENABLED=0 MAIL_DRAFTS_ENABLED=0
run ""; got
[ "$calls" = "drop:default:${URL};" ] || fail "disabled: default should only drop entries ($calls)"

MAIL_MCP_ENABLED=1
for profile in "" browser; do
  run "$profile"; got
  p="${profile:-default}"
  [ "$calls" = "register:${p}:mail:Mail (read-only):${URL}:60:30:${TOOLS};drop:${p}:${URL};pin:${p}:${TOOLS};" ] \
    || fail "${p} should register the four read tools and pin them ($calls)"
done

run api-server; got
[ "$calls" = "drop:api-server:${URL};" ] || fail "api-server must drop, never register ($calls)"

# Flags are read the way the bootstrap reads them: true and yes count.
for on in true TRUE yes YES; do
  MAIL_MCP_ENABLED="$on"
  run browser; got
  case "$calls" in
    "register:browser:mail:"*) ;;
    *) fail "MAIL_MCP_ENABLED=${on} should register ($calls)" ;;
  esac
done

# MAIL_MCP_URL, MAIL_MCP_TIMEOUT and MAIL_MCP_CONNECT_TIMEOUT still override the manifest.
MAIL_MCP_ENABLED=1
export MAIL_MCP_URL="http://elsewhere:9000/mcp" MAIL_MCP_TIMEOUT=90 MAIL_MCP_CONNECT_TIMEOUT=45
run browser; got
[ "$calls" = "register:browser:mail:Mail (read-only):http://elsewhere:9000/mcp:90:45:${TOOLS};drop:browser:http://elsewhere:9000/mcp;pin:browser:${TOOLS};" ] \
  || fail "MAIL_MCP_* overrides the manifest ($calls)"
MAIL_MCP_ENABLED=0
run browser; got
[ "$calls" = "drop:browser:http://elsewhere:9000/mcp;" ] || fail "MAIL_MCP_URL is the URL dropped ($calls)"
unset MAIL_MCP_URL MAIL_MCP_TIMEOUT MAIL_MCP_CONNECT_TIMEOUT

# reconcile_stack_eager_tools re-pins the same tools under both names.
MAIL_MCP_ENABLED=1
pins=" $(addon_mcp_pin_args browser) "
for tool in $TOOLS; do
  case "$pins" in
    *" ${tool} mcp__mail__${tool} "*) ;;
    *) fail "pin args for browser miss ${tool} ($pins)" ;;
  esac
done
case "$(addon_mcp_pin_args api-server)" in
  *mcp__mail__*) fail "no mail pin args on api-server" ;;
esac
MAIL_MCP_ENABLED=0
case "$(addon_mcp_pin_args browser)" in
  *mcp__mail__*) fail "no mail pin args with the flag off" ;;
esac

# Only the four read tools, none of them a write tool.
manifest="${ROOT}/addons/email-local/addon.json"
tools="$(python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); e=[x for x in m["mcp"] if x["name"]=="mail"][0]; print(" ".join(e["tools"]), "|", " ".join(e["writeTools"]))' "$manifest")"
[ "$tools" = "${TOOLS} | " ] || fail "mail tools and writeTools ($tools)"

printf 'OK mail MCP registration\n'
