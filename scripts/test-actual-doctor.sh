#!/usr/bin/env bash
# make doctor's Actual Budget block (scripts/lib/doctor-actual.sh) against a
# stub docker and stub uname/id. No real container, server or budget is used.
#
#   bash scripts/test-actual-doctor.sh
#   ACTUAL_ADMIN_LIVE=1 bash scripts/test-actual-doctor.sh   # also runs actual-admin.mjs
#     in the built image against an unroutable address, to see its timer fire
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
REAL_DOCKER="$(command -v docker || true)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
export STUB="$tmp/stub"
mkdir -p "$tmp/bin" "$tmp/work" "$STUB"

# docker: info works; compose ps -q prints an id for each $STUB/running-<svc>;
# inspect says healthy; exec in hermes logs the URL to $STUB/tools.urls and
# prints $STUB/tools-<host>.json; the admin check prints $STUB/check_word and
# exits with $STUB/check_rc.
cat > "$tmp/bin/docker" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB/docker.argv"
case "$1 ${2:-}" in
  "info "*) exit 0 ;;
  "inspect "*) echo healthy; exit 0 ;;
  "compose ps")
    svc="${*: -1}"
    [[ -e "$STUB/running-$svc" ]] && echo "cid-$svc"
    exit 0 ;;
  "compose exec")
    cat > /dev/null
    for a in "$@"; do
      case "$a" in http://*) printf '%s\n' "$a" >> "$STUB/tools.urls"; host="${a#http://}"; host="${host%%:*}" ;; esac
    done
    cat "$STUB/tools-${host}.json" 2>/dev/null
    exit 0 ;;
  "compose run")
    if [[ "${ACTUAL_SESSION_TOKEN:-}" == tok-bad ]]; then echo login; exit 3; fi
    env | grep '^ACTUAL_' | sort > "$STUB/admin.env"
    cat "$STUB/check_word" 2>/dev/null
    exit "$(cat "$STUB/check_rc" 2>/dev/null || echo 0)" ;;
esac
exit 0
SH
cat > "$tmp/bin/uname" <<'SH'
#!/bin/sh
echo "${STUB_UNAME:-Darwin}"
SH
cat > "$tmp/bin/id" <<'SH'
#!/bin/sh
if [ "${1:-}" = -u ]; then echo "${STUB_UID:-501}"; else exec /usr/bin/id "$@"; fi
SH
chmod +x "$tmp/bin/"*
export PATH="$tmp/bin:$PATH"

# The doctor.sh helpers the block uses.
cat > "$tmp/harness.sh" <<'SH'
set -euo pipefail
ok() { printf '  OK  %s\n' "$1"; }
bad() { printf ' FAIL %s\n' "$1"; }
warn() { printf ' WARN %s\n' "$1"; }
env_get() {
  local val=""
  [[ -f .env ]] && val="$(grep -E "^${1}=" .env | head -1 | cut -d= -f2- || true)"
  echo "${val:-${2:-}}"
}
has_profile() { [[ ",$(env_get COMPOSE_PROFILES core)," == *",$1,"* ]]; }
# shellcheck source=lib/doctor-actual.sh
source "${ROOT}/scripts/lib/doctor-actual.sh"
doctor_actual
SH

# Connected budgets: budget.sh status reads these (python on the host).
export STACK_PYTHON=host BUDGET_DATA_ROOT="$tmp/data"
# budget.sh cds to the repo root, so without this it reads the checkout's own
# .env (ACTUAL_LOCAL_ENABLED and the rest) instead of the test's.
export ENV_FILE="$tmp/work/.env"
sec="$tmp/data/addons/actual/secrets"
mkdir -p "$sec" "$tmp/data/addons/actual/instances"

inst="$tmp/data/addons/actual/instances"

out="$tmp/out"
all="$tmp/all"
: > "$all"
doctor() {
  (cd "$tmp/work" && ROOT="$ROOT" bash "$tmp/harness.sh") > "$out" 2>&1 || fail "the block exited non-zero ($(cat "$out"))"
  cat "$out" >> "$all"
}
has() { grep -qF -- "$2" "$out" || fail "$1 (output: $(cat "$out"))"; }
hasnt() { if grep -qF -- "$2" "$out"; then fail "$1 (output: $(cat "$out"))"; fi; }
setenv() {
  printf 'COMPOSE_PROFILES=core,budget,budget-local\nACTUAL_MCP_ENABLED=1\nACTUAL_LOCAL_ENABLED=1\n' > "$tmp/work/.env"
}
row() { # SLUG SYNC [SERVER] [BIND_DEFAULT]
  local bind=""
  [[ -z "${4:-}" ]] || bind=",\"bind\":{\"default\":\"$4\",\"browser\":\"$4\",\"api-server\":\"ro\"}"
  printf '{"server":"%s","sync_id":"%s"%s}' "${3:-https://budget.example.com}" "$2" "$bind" > "$inst/$1.json"
}
tools() { # host names...
  local host="$1" first=1 n
  shift
  {
    printf 'event: message\ndata: {"result":{"tools":['
    for n in "$@"; do
      [[ "$first" == 1 ]] || printf ','
      first=0
      printf '{"name":"%s","inputSchema":{"type":"object","properties":{"name":{"type":"string"}}}}' "$n"
    done
    printf ']},"jsonrpc":"2.0","id":2}\n'
  } > "$STUB/tools-${host}.json"
}

# Flag off: nothing at all.
printf 'COMPOSE_PROFILES=core\nACTUAL_MCP_ENABLED=0\nACTUAL_LOCAL_ENABLED=1\n' > "$tmp/work/.env"
doctor
[ ! -s "$out" ] || fail "with ACTUAL_MCP_ENABLED=0 the block prints nothing ($(cat "$out"))"
rm -f "$tmp/work/.env"
doctor
[ ! -s "$out" ] || fail "with no .env the block prints nothing"

# No rows: the profiles and the containers, no tools/list and no warning.
setenv
touch "$STUB/running-actual-mcp-rw" "$STUB/running-actual-mcp-ro" "$STUB/running-hermes" "$STUB/running-actual-server"
tools actual-mcp-rw get-accounts get-transactions create-transaction delete-transaction
tools actual-mcp-ro get-accounts get-transactions
doctor
has "the section has a header" "--- Budget (Actual) ---"
has "the budget profile passes" "COMPOSE_PROFILES includes budget"
has "the local profile passes" "COMPOSE_PROFILES includes budget-local"
has "the containers are checked" "actual-mcp-rw is healthy"
has "the local server is checked" "actual-server is healthy"
hasnt "no rows has no FAIL" " FAIL "
hasnt "no rows has no WARN" " WARN "
hasnt "no single-budget file is checked" "budget.env"
hasnt "no ACTUAL_ACCESS check" "ACTUAL_ACCESS"
[ ! -e "$STUB/tools.urls" ] || fail "no rows runs no tools/list ($(cat "$STUB/tools.urls"))"

# Connected budgets: one line each; only the expired one warns. tools/list runs
# per ok row on its /<slug>/mcp path: ro always, rw for a write row.
row main s1 https://budget.example.com rw
row second s2
row third s3 https://budget.example.com ro
printf 'tok-good' > "$sec/main.session_token"
printf 'tok-bad' > "$sec/second.session_token"
printf 'tok-good' > "$sec/third.session_token"
doctor
has "a good row is OK" "  OK  budget-main: ok"
has "a read row is OK" "  OK  budget-third: ok"
has "an expired row warns" " WARN budget-second: token_expired"
[ "$(grep -c 'WARN budget-' "$out")" = 1 ] || fail "exactly one budget warning ($(cat "$out"))"
hasnt "a healthy set of rows has no FAIL" " FAIL "
hasnt "a row's output holds no token" "tok-"
has "rw answers tools/list for a write row" "budget-main on actual-mcp-rw answers tools/list (4 tools)"
has "ro lists no write tool" "budget-main on actual-mcp-ro lists 2 tools and none of the 32 write tools"
has "a read row's ro is checked" "budget-third on actual-mcp-ro lists 2 tools"
hasnt "a read row's rw isn't checked" "budget-third on actual-mcp-rw"
hasnt "an expired row's tools aren't checked" "budget-second on"
grep -qx 'http://actual-mcp-ro:3000/main/mcp' "$STUB/tools.urls" || fail "tools/list uses the row's path ($(cat "$STUB/tools.urls"))"
grep -qx 'http://actual-mcp-rw:3000/main/mcp' "$STUB/tools.urls" || fail "tools/list on rw uses the row's path"
if grep -q ':3000/mcp$' "$STUB/tools.urls"; then fail "no tools/list on the legacy /mcp path"; fi
grep -q 'compose exec -T hermes' "$STUB/docker.argv" || fail "tools/list runs inside the hermes container"

# The ro instance lists a write tool.
tools actual-mcp-ro get-accounts create-transaction set-budget-amount
doctor
has "a write tool on ro fails" "budget-main on actual-mcp-ro lists write tools: create-transaction set-budget-amount"
tools actual-mcp-ro get-accounts get-transactions

# A row gives no tools.
rm -f "$STUB/tools-actual-mcp-rw.json"
doctor
has "no tools on rw fails" "budget-main on actual-mcp-rw gave no tools on tools/list"
tools actual-mcp-rw get-accounts get-transactions create-transaction delete-transaction

# Not running: warnings, no exec.
rm -f "$STUB/running-actual-mcp-ro" "$STUB/running-hermes"
: > "$STUB/docker.argv"
doctor
has "a stopped ro instance warns" "actual-mcp-ro not running"
has "a stopped hermes warns" "hermes not running"
if grep -q 'compose exec' "$STUB/docker.argv"; then fail "no tools/list without hermes"; fi
touch "$STUB/running-actual-mcp-ro" "$STUB/running-hermes"

# A local row while the local server is off: one warning with the way back
# on, and the remote rows still read ok.
row mine s4 local
printf 'COMPOSE_PROFILES=core,budget\nACTUAL_MCP_ENABLED=1\nACTUAL_LOCAL_ENABLED=0\n' > "$tmp/work/.env"
doctor
has "a local row with the server off warns server_off" " WARN budget-mine: server_off"
has "server_off says how to turn the local server back on" "echo '{\"local\": true}' | scripts/addon.sh configure actual, then scripts/addon.sh enable actual (add --reinit"
[ "$(grep -c 'WARN budget-mine' "$out")" = 1 ] || fail "exactly one server_off warning ($(cat "$out"))"
has "a remote row beside it is still checked" "  OK  budget-main: ok"
hasnt "the local server isn't checked while off" "actual-server"
rm -f "$inst/mine.json"
setenv

# Rows on Linux as a uid other than 1000: the containers can't read the secrets.
STUB_UNAME=Linux STUB_UID=1001 doctor
has "rows on Linux uid 1001 fail" "read the connected budgets' secret files as uid 1000"
STUB_UNAME=Linux STUB_UID=1000 doctor
hasnt "rows on Linux uid 1000 pass" "secret files as uid 1000"
rm -f "$inst"/*.json "$sec"/*.session_token
STUB_UNAME=Linux STUB_UID=1001 doctor
hasnt "no rows on Linux uid 1001 pass" "secret files as uid 1000"

# Profile missing.
printf 'COMPOSE_PROFILES=core\nACTUAL_MCP_ENABLED=1\n' > "$tmp/work/.env"
doctor
has "a missing budget profile fails" "'budget' not in COMPOSE_PROFILES"
printf 'COMPOSE_PROFILES=core,budget\nACTUAL_MCP_ENABLED=1\nACTUAL_LOCAL_ENABLED=1\n' > "$tmp/work/.env"
doctor
has "a missing budget-local profile fails" "'budget-local' not in COMPOSE_PROFILES"

# No token in any output.
for s in tok-bad tok-good; do
  if grep -qF -- "$s" "$all"; then fail "doctor output contains a token"; fi
  if grep -qF -- "$s" "$STUB/docker.argv"; then fail "docker argv contains a token"; fi
done

# Opt-in: the real timer in actual-admin.mjs. 10.255.255.1 is unroutable, so
# the connection hangs until ACTUAL_ADMIN_TIMEOUT_S=1 fires.
if [[ "${ACTUAL_ADMIN_LIVE:-}" == 1 ]]; then
  image="assistant-actual-mcp:local"
  if [[ -z "$REAL_DOCKER" ]] || ! "$REAL_DOCKER" image inspect "$image" >/dev/null 2>&1; then
    fail "ACTUAL_ADMIN_LIVE=1 needs docker and $image (make up builds it)"
  fi
  start=$SECONDS
  rc=0
  word="$(ACTUAL_SERVER_URL=http://10.255.255.1:5006 ACTUAL_PASSWORD=x ACTUAL_BUDGET_SYNC_ID=x ACTUAL_ADMIN_TIMEOUT_S=1 \
    "$REAL_DOCKER" run --rm -e ACTUAL_SERVER_URL -e ACTUAL_PASSWORD -e ACTUAL_BUDGET_SYNC_ID -e ACTUAL_ADMIN_TIMEOUT_S \
    --entrypoint node "$image" /opt/actual-admin.mjs check 2>/dev/null)" || rc=$?
  [[ "$rc" == 4 && "$word" == timeout ]] || fail "live: expected timeout and exit 4, got '${word}' and ${rc}"
  printf 'ok: live actual-admin.mjs timed out after 1 s (exit 4, %s s with container start)\n' "$((SECONDS - start))"
fi

printf 'OK doctor Actual block\n'
