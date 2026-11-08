#!/usr/bin/env bash
# make doctor's Actual Budget block (scripts/lib/doctor-actual.sh) against a
# stub docker and stub uname/id. No real container, server or budget is used.
#
#   bash scripts/test-actual-doctor.sh
#   ACTUAL_ADMIN_LIVE=1 bash scripts/test-actual-doctor.sh   # also runs actual-admin.mjs
#     in the built image against an unroutable address, to see its timer fire
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REAL_DOCKER="$(command -v docker || true)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
export STUB="$tmp/stub"
mkdir -p "$tmp/bin" "$tmp/work/actual" "$STUB"
f="$tmp/work/actual/budget.env"

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

# docker: info works; compose ps -q prints an id for each $STUB/running-<svc>;
# inspect says healthy; exec in hermes prints $STUB/tools-<host>.json; the
# admin check prints $STUB/check_word and exits with $STUB/check_rc.
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
      case "$a" in http://*) host="${a#http://}"; host="${host%%:*}" ;; esac
    done
    cat "$STUB/tools-${host}.json" 2>/dev/null
    exit 0 ;;
  "compose run")
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

out="$tmp/out"
all="$tmp/all"
: > "$all"
doctor() {
  (cd "$tmp/work" && ROOT="$ROOT" bash "$tmp/harness.sh") > "$out" 2>&1 || fail "the block exited non-zero ($(cat "$out"))"
  cat "$out" >> "$all"
}
has() { grep -qF -- "$2" "$out" || fail "$1 (output: $(cat "$out"))"; }
hasnt() { if grep -qF -- "$2" "$out"; then fail "$1 (output: $(cat "$out"))"; fi; }
setenv() { # extra .env lines
  {
    printf 'COMPOSE_PROFILES=core,budget,budget-local\nACTUAL_MCP_ENABLED=1\nACTUAL_LOCAL_ENABLED=1\n'
    printf 'ACTUAL_BUDGET_FILE=./actual/budget.env\n'
    printf '%s\n' "$@"
  } > "$tmp/work/.env"
}
budget_file() { # server url
  printf 'ACTUAL_SERVER_URL=%s\nACTUAL_WEB_URL=http://localhost:5006/\nACTUAL_PASSWORD=pw-SECRET\nACTUAL_BUDGET_SYNC_ID=sync-1\nACTUAL_BUDGET_ENCRYPTION_PASSWORD=enc-SECRET\n' \
    "${1:-http://actual-server:5006}" > "$f"
  chmod 600 "$f"
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

# Healthy: everything passes, both instances answer, ro has no write tool.
setenv
budget_file
touch "$STUB/running-actual-mcp-rw" "$STUB/running-actual-mcp-ro" "$STUB/running-hermes" "$STUB/running-actual-server"
tools actual-mcp-rw get-accounts get-transactions create-transaction delete-transaction
tools actual-mcp-ro get-accounts get-transactions
doctor
hasnt "a healthy budget has no FAIL" " FAIL "
hasnt "a healthy budget has no WARN" " WARN "
has "the section has a header" "--- Budget (Actual) ---"
has "the file check passes" "mode 600"
has "the credentials check passes" "budget sync-1 opens"
has "rw answers tools/list" "actual-mcp-rw answers tools/list (4 tools)"
has "ro lists no write tool" "actual-mcp-ro lists 2 tools and none of the 32 write tools"
grep -q 'ACTUAL_PASSWORD=pw-SECRET' "$STUB/admin.env" || fail "the check gets the file's password in docker's environment"
grep -q 'ACTUAL_BUDGET_ENCRYPTION_PASSWORD=enc-SECRET' "$STUB/admin.env" || fail "the check gets the encryption password"
grep -q 'compose exec -T hermes' "$STUB/docker.argv" || fail "tools/list runs inside the hermes container"

# A missing budget file.
rm -f "$f"
doctor
has "a missing file fails" " FAIL "
has "a missing file says what to run" "run make budget-init or make budget-add"
hasnt "no credential check without a file" "opens"

# ACTUAL_BUDGET_FILE unset: the default path, and a FAIL that compose mounts no-budget.env.
printf 'COMPOSE_PROFILES=core,budget\nACTUAL_MCP_ENABLED=1\n' > "$tmp/work/.env"
mkdir -p "$tmp/work/compose/actual"
doctor
has "unset ACTUAL_BUDGET_FILE fails" "ACTUAL_BUDGET_FILE is unset"
has "unset ACTUAL_BUDGET_FILE checks the default path" "compose/actual/budget.env is missing"

# Mode 644.
setenv
budget_file
chmod 644 "$f"
doctor
has "mode 644 fails" " FAIL "
has "mode 644 is named" "mode 644"
chmod 600 "$f"

# Linux with a uid other than 1000.
STUB_UNAME=Linux STUB_UID=1001 doctor
has "Linux uid 1001 fails" "uid 1000"
STUB_UNAME=Linux STUB_UID=1000 doctor
hasnt "Linux uid 1000 passes" "uid 1000"

# A file missing a key.
grep -v '^ACTUAL_BUDGET_SYNC_ID=' "$f" > "$f.new" && mv "$f.new" "$f" && chmod 600 "$f"
doctor
has "a missing sync id fails" "has no ACTUAL_BUDGET_SYNC_ID"
budget_file

# ACTUAL_ACCESS.
setenv ACTUAL_ACCESS=Read
doctor
has "ACTUAL_ACCESS=Read fails, naming the value" "ACTUAL_ACCESS=Read"
grep -F 'ACTUAL_ACCESS=Read' "$out" | grep -q FAIL || fail "ACTUAL_ACCESS=Read is a FAIL"
setenv ACTUAL_ACCESS=read
doctor
hasnt "ACTUAL_ACCESS=read passes" " FAIL "

# The server refuses the password; a wrong encryption password; no answer.
setenv
printf 'login\n' > "$STUB/check_word"; printf '3\n' > "$STUB/check_rc"
doctor
has "a refused password fails" "the server password changed"
has "the remedy covers a remote server" "re-run make budget-add FORCE=1 (remote)"
has "the remedy covers the local server" "make budget-remove and then make budget-init, which asks for it (local"
has "the local remedy covers an encrypted budget" "export ACTUAL_BUDGET_ENCRYPTION_PASSWORD first if the budget is encrypted"
printf 'encryption\n' > "$STUB/check_word"; printf '2\n' > "$STUB/check_rc"
doctor
has "a wrong encryption password fails" "ACTUAL_BUDGET_ENCRYPTION_PASSWORD"
grep -F 'ACTUAL_BUDGET_ENCRYPTION_PASSWORD' "$out" | grep -q FAIL || fail "the encryption check is a FAIL"
printf 'timeout\n' > "$STUB/check_word"; printf '4\n' > "$STUB/check_rc"
doctor
has "a timeout warns, naming the server" " WARN no answer from http://actual-server:5006 within 45 s"
hasnt "a timeout is not a FAIL" " FAIL "
budget_file https://user:url-SECRET@budget.example.com
ACTUAL_ADMIN_TIMEOUT_S=3 doctor
has "a timeout masks user:password@ and uses ACTUAL_ADMIN_TIMEOUT_S" "no answer from https://***@budget.example.com within 3 s"
budget_file
printf '1\n' > "$STUB/check_rc"; : > "$STUB/check_word"
doctor
has "another check failure warns" "couldn't open budget sync-1"
rm -f "$STUB/check_word" "$STUB/check_rc"

# The ro instance lists a write tool.
tools actual-mcp-ro get-accounts create-transaction set-budget-amount
doctor
has "a write tool on ro fails" "actual-mcp-ro lists write tools: create-transaction set-budget-amount"
tools actual-mcp-ro get-accounts get-transactions

# Not running: warnings, no exec.
rm -f "$STUB/running-actual-mcp-ro" "$STUB/running-hermes"
: > "$STUB/docker.argv"
doctor
has "a stopped ro instance warns" "actual-mcp-ro not running"
if grep -q 'compose exec' "$STUB/docker.argv"; then fail "no tools/list without hermes"; fi
touch "$STUB/running-actual-mcp-ro" "$STUB/running-hermes"

# Local server off while the file points at it.
setenv
printf 'COMPOSE_PROFILES=core,budget\nACTUAL_MCP_ENABLED=1\nACTUAL_LOCAL_ENABLED=0\nACTUAL_BUDGET_FILE=./actual/budget.env\n' > "$tmp/work/.env"
doctor
has "local off with an actual-server URL warns" " WARN "
has "the warning names ACTUAL_LOCAL_ENABLED" "ACTUAL_LOCAL_ENABLED=0"
budget_file https://budget.example.com
doctor
hasnt "local off with a remote URL doesn't warn about it" "ACTUAL_LOCAL_ENABLED=0"

# Profile missing.
printf 'COMPOSE_PROFILES=core\nACTUAL_MCP_ENABLED=1\nACTUAL_BUDGET_FILE=./actual/budget.env\n' > "$tmp/work/.env"
doctor
has "a missing budget profile fails" "'budget' not in COMPOSE_PROFILES"

# No password in any output.
for s in pw-SECRET enc-SECRET url-SECRET; do
  if grep -qF -- "$s" "$all"; then fail "doctor output contains a password"; fi
  if grep -qF -- "$s" "$STUB/docker.argv"; then fail "docker argv contains a password"; fi
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
