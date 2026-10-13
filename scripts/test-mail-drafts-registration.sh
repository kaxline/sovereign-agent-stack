#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# Which Hermes profiles hermes-*-bootstrap gives the mail drafts MCP.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"

# shellcheck disable=SC1090
eval "$(sed -n '/^register_mail_drafts_mcp_servers() {/,/^}/p' "$bootstrap")"
eval "$(grep -E '^MAIL_DRAFTS_MCP_(URL|TIMEOUT|CONNECT_TIMEOUT|TOOLS)=' "$bootstrap")"

calls=""
log() { :; }
register_mcp_server() { calls="${calls}register:${1:-default}:${2}:$(shift 6; echo "$*");"; }
pin_mcp_tools() { calls="${calls}pin:${1:-default};"; }
drop_mcp_entries_for_url() { calls="${calls}drop:${1:-default}:${3};"; }

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

run() {
  calls=""
  register_mail_drafts_mcp_servers "$1"
}

MAIL_DRAFTS_ENABLED=0
run ""
[ "$calls" = "drop:default:http://mail-drafts-mcp:8000/mcp;" ] || fail "disabled: default should only drop entries ($calls)"

MAIL_DRAFTS_ENABLED=1
for profile in "" browser; do
  run "$profile"
  case "$calls" in
    "register:${profile:-default}:drafts:mail_draft_create mail_draft_status;pin:${profile:-default};") ;;
    *) fail "${profile:-default} should register the two draft tools ($calls)" ;;
  esac
done

run api-server
[ "$calls" = "drop:api-server:http://mail-drafts-mcp:8000/mcp;" ] || fail "api-server must drop, never register ($calls)"

for tool in $MAIL_DRAFTS_MCP_TOOLS; do
  case "$tool" in
    mail_draft_create|mail_draft_status) ;;
    *) fail "unexpected tool in allowlist: $tool" ;;
  esac
done

printf 'OK mail drafts MCP registration\n'
