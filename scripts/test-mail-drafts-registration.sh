#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# Which Hermes profiles hermes-*-bootstrap gives the mail drafts MCP, and which
# tools. Registration goes through the generic add-on path
# (register_addon_mcp_servers reading addons/email-local/addon.json, whose
# drafts entry has when: {gate: MAIL_DRAFTS_ENABLED}); the calls it makes must
# match what register_mail_drafts_mcp_servers made before the manifest
# replaced it.
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
profile_env_file() { :; }
remove_env_key() { :; }

# The real addons/ dir: other add-ons add their own calls, so the stubs record
# only calls about the two mail servers. The plan is read in a pipeline, so
# the record comes back through a file rather than the subshell's $calls.
URL="http://mail-drafts-mcp:8000/mcp"
MAIL_URL="http://mail-mcp:8000/mcp"
record="$(mktemp)"
trap 'rm -f "$record"' EXIT
run() {
  register_addon_mcp_servers "$1" > /dev/null
}
register_mcp_server() {
  case "$2" in mail|drafts) ;; *) return 0 ;; esac
  printf 'register:%s:%s:%s:%s:%s:%s:%s;' "${1:-default}" "$2" "$3" "$4" "$5" "$6" "$(shift 6; echo "$*")" >> "$record"
}
hermes_config_set() {
  case "$2" in mcp_servers.mail.*|mcp_servers.drafts.*) printf 'set:%s:%s=%s;' "${1:-default}" "$2" "$3" >> "$record" ;; esac
}
pin_mcp_tools() {
  case "$2" in mail|drafts) ;; *) return 0 ;; esac
  printf 'pin:%s:%s:%s;' "${1:-default}" "$2" "$(shift 2; echo "$*")" >> "$record"
}
drop_mcp_entries_for_url() {
  case "$3" in "$URL"|"$MAIL_URL") printf 'drop:%s:%s;' "${1:-default}" "$3" >> "$record" ;; esac
}
got() { calls="$(cat "$record")"; : > "$record"; }
# Only the drafts calls of a record: with MAIL_MCP_ENABLED=0 the mail entry adds
# its own drop, which the drafts cases aren't about.
drafts_only() { printf '%s' "$1" | tr ';' '\n' | grep -E '^(register|pin):[^:]*:drafts:|^drop:[^:]*:http://mail-drafts-mcp' | tr '\n' ';'; }

TOOLS="mail_draft_create mail_draft_status"

export MAIL_MCP_ENABLED=0 MAIL_DRAFTS_ENABLED=0
run ""; got
[ "$(drafts_only "$calls")" = "drop:default:${URL};" ] || fail "disabled: default should only drop entries ($calls)"

MAIL_DRAFTS_ENABLED=1
for profile in "" browser; do
  run "$profile"; got
  p="${profile:-default}"
  [ "$(drafts_only "$calls")" = "register:${p}:drafts:Mail drafts (no sending):${URL}:60:30:${TOOLS};drop:${p}:${URL};pin:${p}:drafts:${TOOLS};" ] \
    || fail "${p} should register the two draft tools and pin them ($calls)"
done

run api-server; got
[ "$(drafts_only "$calls")" = "drop:api-server:${URL};" ] || fail "api-server must drop, never register ($calls)"

# The drafts gate stands on its own: drafts on and mail off registers drafts
# and drops mail, on both interactive profiles.
MAIL_MCP_ENABLED=0 MAIL_DRAFTS_ENABLED=1
for profile in "" browser; do
  run "$profile"; got
  p="${profile:-default}"
  [ "$calls" = "drop:${p}:${MAIL_URL};register:${p}:drafts:Mail drafts (no sending):${URL}:60:30:${TOOLS};drop:${p}:${URL};pin:${p}:drafts:${TOOLS};" ] \
    || fail "${p}: drafts on, mail off registers drafts and drops mail ($calls)"
done

# MAIL_DRAFTS_MCP_URL and its timeouts still override the manifest.
export MAIL_DRAFTS_MCP_URL="http://elsewhere:9000/mcp" MAIL_DRAFTS_MCP_TIMEOUT=90 MAIL_DRAFTS_MCP_CONNECT_TIMEOUT=45
run browser; got
case "$calls" in
  *"register:browser:drafts:Mail drafts (no sending):http://elsewhere:9000/mcp:90:45:${TOOLS};"*) ;;
  *) fail "MAIL_DRAFTS_MCP_* overrides the manifest ($calls)" ;;
esac
unset MAIL_DRAFTS_MCP_URL MAIL_DRAFTS_MCP_TIMEOUT MAIL_DRAFTS_MCP_CONNECT_TIMEOUT

# reconcile_stack_eager_tools re-pins the same tools under both names.
pins=" $(addon_mcp_pin_args browser) "
for tool in $TOOLS; do
  case "$pins" in
    *" ${tool} mcp__drafts__${tool} "*) ;;
    *) fail "pin args for browser miss ${tool} ($pins)" ;;
  esac
done
case "$(addon_mcp_pin_args api-server)" in
  *mcp__drafts__*) fail "no drafts pin args on api-server" ;;
esac
MAIL_DRAFTS_ENABLED=0
case "$(addon_mcp_pin_args browser)" in
  *mcp__drafts__*) fail "no drafts pin args with the gate off" ;;
esac

# Only the two draft tools; creating a draft is the one write.
manifest="${ROOT}/addons/email-local/addon.json"
tools="$(python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); e=[x for x in m["mcp"] if x["name"]=="drafts"][0]; print(" ".join(e["tools"]), "|", " ".join(e["writeTools"]))' "$manifest")"
[ "$tools" = "${TOOLS} | mail_draft_create" ] || fail "drafts tools and writeTools ($tools)"

printf 'OK mail drafts MCP registration\n'
