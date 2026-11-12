#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# Which Hermes profiles hermes-*-bootstrap gives the messages MCP, and which tools.
# Registration goes through the generic add-on path (register_addon_mcp_servers
# reading addons/messages/addon.json); the calls it makes must match what
# register_messages_mcp_servers made before the manifest replaced it.
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
profile_env_file() { :; }
remove_env_key() { :; }

# The real addons/ dir: other add-ons add their own calls, so the stubs record
# only calls about the messages server. The plan is read in a pipeline, so the
# record comes back through a file rather than the subshell's $calls.
URL="http://messages-mcp:8000/mcp"
record="$(mktemp)"
trap 'rm -f "$record"' EXIT
run() {
  register_addon_mcp_servers "$1" > /dev/null
}
register_mcp_server() {
  [ "$2" = messages ] || return 0
  printf 'register:%s:%s:%s:%s:%s:%s:%s;' "${1:-default}" "$2" "$3" "$4" "$5" "$6" "$(shift 6; echo "$*")" >> "$record"
}
hermes_config_set() {
  case "$2" in mcp_servers.messages.*) printf 'set:%s:%s=%s;' "${1:-default}" "$2" "$3" >> "$record" ;; esac
}
pin_mcp_tools() {
  [ "$2" = messages ] || return 0
  printf 'pin:%s:%s;' "${1:-default}" "$(shift 2; echo "$*")" >> "$record"
}
drop_mcp_entries_for_url() {
  case "$3" in "$URL"|http://elsewhere:*) printf 'drop:%s:%s;' "${1:-default}" "$3" >> "$record" ;; esac
}
got() { calls="$(cat "$record")"; : > "$record"; }

READ="messages_status messages_chats messages_search messages_conversation messages_get"

export MESSAGES_MCP_ENABLED=0
for profile in "" browser api-server; do
  run "$profile"; got
  [ "$calls" = "drop:${profile:-default}:${URL};" ] || fail "disabled: ${profile:-default} should only drop entries ($calls)"
done

MESSAGES_MCP_ENABLED=1
run ""; got
[ "$calls" = "register:default:messages:Messages (read-only):${URL}:60:30:${READ} messages_triage_batch;drop:default:${URL};pin:default:${READ};" ] \
  || fail "default registers the read tools plus the triage batch, and pins only the read tools ($calls)"
run browser; got
[ "$calls" = "register:browser:messages:Messages (read-only):${URL}:60:30:${READ};drop:browser:${URL};pin:browser:${READ};" ] \
  || fail "browser registers the read tools only ($calls)"
run api-server; got
[ "$calls" = "drop:api-server:${URL};" ] || fail "api-server must drop, never register ($calls)"

# Flags are read the way the bootstrap reads them: true and yes count.
for on in true TRUE yes YES; do
  MESSAGES_MCP_ENABLED="$on"
  run browser; got
  case "$calls" in
    "register:browser:messages:"*) ;;
    *) fail "MESSAGES_MCP_ENABLED=${on} should register ($calls)" ;;
  esac
done

# MESSAGES_MCP_URL, MESSAGES_MCP_TIMEOUT and MESSAGES_MCP_CONNECT_TIMEOUT still override the manifest.
MESSAGES_MCP_ENABLED=1
export MESSAGES_MCP_URL="http://elsewhere:9000/mcp" MESSAGES_MCP_TIMEOUT=90 MESSAGES_MCP_CONNECT_TIMEOUT=45
run browser; got
[ "$calls" = "register:browser:messages:Messages (read-only):http://elsewhere:9000/mcp:90:45:${READ};drop:browser:http://elsewhere:9000/mcp;pin:browser:${READ};" ] \
  || fail "MESSAGES_MCP_* overrides the manifest ($calls)"
MESSAGES_MCP_ENABLED=0
run browser; got
[ "$calls" = "drop:browser:http://elsewhere:9000/mcp;" ] || fail "MESSAGES_MCP_URL is the URL dropped ($calls)"
unset MESSAGES_MCP_URL MESSAGES_MCP_TIMEOUT MESSAGES_MCP_CONNECT_TIMEOUT

# reconcile_stack_eager_tools re-pins the read tools under both names, never
# the triage batch.
MESSAGES_MCP_ENABLED=1
for profile in "" browser; do
  pins=" $(addon_mcp_pin_args "$profile") "
  for tool in $READ; do
    case "$pins" in
      *" ${tool} mcp__messages__${tool} "*) ;;
      *) fail "pin args for ${profile:-default} miss ${tool} ($pins)" ;;
    esac
  done
  case "$pins" in
    *messages_triage_batch*) fail "messages_triage_batch is never pinned (${profile:-default})" ;;
  esac
done
case "$(addon_mcp_pin_args api-server)" in
  *mcp__messages__*) fail "no messages pin args on api-server" ;;
esac
MESSAGES_MCP_ENABLED=0
case "$(addon_mcp_pin_args browser)" in
  *mcp__messages__*) fail "no messages pin args with the flag off" ;;
esac

# Personal data: no api-server, no cron, no sending.
manifest="${ROOT}/addons/messages/addon.json"
perms="$(python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); p=m["permissions"]; print(p["reads"], p["sends"], p["cron"], sorted(m["mcp"]["profiles"].items()))' "$manifest")"
[ "$perms" = "['messages'] False False [('browser', 'ro'), ('default', 'rw')]" ] \
  || fail "messages reads messages, never sends, stays out of cron and off api-server ($perms)"

grep -q '"messages-mcp:8000"' "$bootstrap" || fail "the cron fence must cover messages-mcp"
grep -q '"cron_job": "messages-triage"' "$bootstrap" || fail "the webhook route must fire messages-triage"
# Not in a pipeline: a missing function must fail here, not pass the grep.
webhook_src="$(fn_source "$bootstrap" configure_messages_webhook)" || exit 1
if printf '%s\n' "$webhook_src" | grep -q 'PROMPT = .*{chat\|PROMPT = .*{senders'; then
  fail "the webhook prompt must not carry chat or sender names"
fi

printf 'OK messages MCP registration\n'
