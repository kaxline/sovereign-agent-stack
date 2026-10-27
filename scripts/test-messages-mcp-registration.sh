#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# Which Hermes profiles hermes-*-bootstrap gives the messages MCP, and which tools.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"

# shellcheck disable=SC1090
eval "$(sed -n '/^register_messages_mcp_servers() {/,/^}/p' "$bootstrap")"
eval "$(grep -E '^MESSAGES_(MCP_URL|MCP_TIMEOUT|MCP_CONNECT_TIMEOUT|MCP_TOOLS|TRIAGE_TOOL)=' "$bootstrap")"

calls=""
log() { :; }
register_mcp_server() { calls="${calls}register:${1:-default}:${2}:$(shift 6; echo "$*");"; }
pin_mcp_tools() { calls="${calls}pin:${1:-default}:$(shift 2; echo "$*");"; }
drop_mcp_entries_for_url() { calls="${calls}drop:${1:-default}:${3};"; }

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

run() {
  calls=""
  register_messages_mcp_servers "$1"
}

READ="messages_status messages_chats messages_search messages_conversation messages_get"

MESSAGES_MCP_ENABLED=0
run ""
[ "$calls" = "drop:default:http://messages-mcp:8000/mcp;" ] || fail "disabled: default should only drop entries ($calls)"

MESSAGES_MCP_ENABLED=1
run ""
[ "$calls" = "register:default:messages:${READ} messages_triage_batch;pin:default:${READ};" ] \
  || fail "default registers the read tools plus the triage batch, and pins only the read tools ($calls)"
run browser
[ "$calls" = "register:browser:messages:${READ};pin:browser:${READ};" ] \
  || fail "browser registers the read tools only ($calls)"
run api-server
[ "$calls" = "drop:api-server:http://messages-mcp:8000/mcp;" ] || fail "api-server must drop, never register ($calls)"

for tool in $MESSAGES_MCP_TOOLS $MESSAGES_TRIAGE_TOOL; do
  case "$tool" in
    messages_status|messages_chats|messages_search|messages_conversation|messages_get|messages_triage_batch) ;;
    *) fail "unexpected tool in allowlist: $tool" ;;
  esac
done

grep -q '"messages-mcp:8000"' "$bootstrap" || fail "the cron fence must cover messages-mcp"
grep -q '"cron_job": "messages-triage"' "$bootstrap" || fail "the webhook route must fire messages-triage"
if sed -n '/^configure_messages_webhook() {/,/^}/p' "$bootstrap" | grep -q 'PROMPT = .*{chat\|PROMPT = .*{senders'; then
  fail "the webhook prompt must not carry chat or sender names"
fi

printf 'OK messages MCP registration\n'
