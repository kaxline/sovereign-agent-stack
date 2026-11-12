#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# Which Hermes profiles hermes-*-bootstrap gives the IMAP email add-on's two
# MCP servers (addons/email/addon.json), and which tools. EMAIL_ENABLED
# registers `email`; the EMAIL_DRAFTS_ENABLED gate registers `email-drafts`.
# Neither ever registers on api-server.
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

[ -f "${ADDONS_DIR}/email/addon.json" ] || fail "addons/email/addon.json is missing"

# The real addons/ dir: other add-ons add their own calls, so the stubs record
# only calls about the two email servers. The plan is read in a pipeline, so
# the record comes back through a file rather than the subshell's $calls.
URL="http://email-mcp:8000/mcp"
DRAFTS_URL="http://email-drafts-mcp:8000/mcp"
record="$(mktemp)"
trap 'rm -f "$record"' EXIT
run() {
  register_addon_mcp_servers "$1" > /dev/null
}
register_mcp_server() {
  case "$2" in email|email-drafts) ;; *) return 0 ;; esac
  printf 'register:%s:%s:%s:%s:%s:%s:%s;' "${1:-default}" "$2" "$3" "$4" "$5" "$6" "$(shift 6; echo "$*")" >> "$record"
}
hermes_config_set() {
  case "$2" in mcp_servers.email.*|mcp_servers.email-drafts.*) printf 'set:%s:%s=%s;' "${1:-default}" "$2" "$3" >> "$record" ;; esac
}
pin_mcp_tools() {
  case "$2" in email|email-drafts) ;; *) return 0 ;; esac
  printf 'pin:%s:%s:%s;' "${1:-default}" "$2" "$(shift 2; echo "$*")" >> "$record"
}
drop_mcp_entries_for_url() {
  case "$3" in "$URL"|"$DRAFTS_URL") printf 'drop:%s:%s;' "${1:-default}" "$3" >> "$record" ;; esac
}
got() { calls="$(cat "$record")"; : > "$record"; }

TOOLS="mail_status mail_search mail_get mail_thread"
DRAFT_TOOLS="mail_draft_create mail_draft_status"
REG="register:%s:email:Email (read-only):${URL}:60:30:${TOOLS};drop:%s:${URL};pin:%s:email:${TOOLS};"
DRAFTS_REG="register:%s:email-drafts:Email drafts (no sending):${DRAFTS_URL}:60:30:${DRAFT_TOOLS};drop:%s:${DRAFTS_URL};pin:%s:email-drafts:${DRAFT_TOOLS};"

# Off: every profile drops both URLs and registers nothing.
export EMAIL_ENABLED=0 EMAIL_DRAFTS_ENABLED=0
for profile in "" browser api-server; do
  run "$profile"; got
  p="${profile:-default}"
  [ "$calls" = "drop:${p}:${URL};drop:${p}:${DRAFTS_URL};" ] || fail "off: ${p} should drop both URLs ($calls)"
done

# On: email on default and browser; email-drafts still dropped.
EMAIL_ENABLED=1
for profile in "" browser; do
  run "$profile"; got
  p="${profile:-default}"
  # shellcheck disable=SC2059
  want="$(printf "$REG" "$p" "$p" "$p")drop:${p}:${DRAFTS_URL};"
  [ "$calls" = "$want" ] || fail "on: ${p} should register email and drop email-drafts ($calls)"
done

# The gate adds email-drafts.
EMAIL_DRAFTS_ENABLED=1
for profile in "" browser; do
  run "$profile"; got
  p="${profile:-default}"
  # shellcheck disable=SC2059
  want="$(printf "$REG" "$p" "$p" "$p")$(printf "$DRAFTS_REG" "$p" "$p" "$p")"
  [ "$calls" = "$want" ] || fail "gate on: ${p} should register email and email-drafts ($calls)"
done

# api-server only ever drops, whatever the flags say.
for EMAIL_ENABLED in 0 1; do
  for EMAIL_DRAFTS_ENABLED in 0 1; do
    run api-server; got
    [ "$calls" = "drop:api-server:${URL};drop:api-server:${DRAFTS_URL};" ] \
      || fail "api-server must drop, never register (EMAIL_ENABLED=${EMAIL_ENABLED} EMAIL_DRAFTS_ENABLED=${EMAIL_DRAFTS_ENABLED}: $calls)"
  done
done

# Flags are read the way the bootstrap reads them: true and yes count.
EMAIL_DRAFTS_ENABLED=0
for on in true YES; do
  EMAIL_ENABLED="$on"
  run browser; got
  case "$calls" in
    "register:browser:email:"*) ;;
    *) fail "EMAIL_ENABLED=${on} should register ($calls)" ;;
  esac
done

# reconcile_stack_eager_tools re-pins the same tools under both names.
EMAIL_ENABLED=1 EMAIL_DRAFTS_ENABLED=1
pins=" $(addon_mcp_pin_args browser) "
for tool in $TOOLS; do
  case "$pins" in
    *" ${tool} mcp__email__${tool} "*) ;;
    *) fail "pin args for browser miss ${tool} ($pins)" ;;
  esac
done
for tool in $DRAFT_TOOLS; do
  case "$pins" in
    *" ${tool} mcp__email_drafts__${tool} "*) ;;
    *) fail "pin args for browser miss email-drafts ${tool} ($pins)" ;;
  esac
done
case "$(addon_mcp_pin_args api-server)" in
  *mcp__email__*|*mcp__email_drafts__*) fail "no email pin args on api-server" ;;
esac
EMAIL_ENABLED=0 EMAIL_DRAFTS_ENABLED=0
case "$(addon_mcp_pin_args browser)" in
  *mcp__email__*|*mcp__email_drafts__*) fail "no email pin args with the flags off" ;;
esac

# The read server has no write tool; creating a draft is the one write.
manifest="${ADDONS_DIR}/email/addon.json"
tools="$(python3 -c 'import json,sys
m=json.load(open(sys.argv[1]))
for n in ("email", "email-drafts"):
    e=[x for x in m["mcp"] if x["name"]==n][0]
    print(" ".join(e["tools"]), "|", " ".join(e["writeTools"]), "|", " ".join(e["pin"]), "|", "env" in e)' "$manifest")"
[ "$tools" = "${TOOLS} |  | ${TOOLS} | False
${DRAFT_TOOLS} | mail_draft_create | ${DRAFT_TOOLS} | False" ] || fail "email tools, writeTools, pins and env ($tools)"

printf 'OK email MCP registration\n'
