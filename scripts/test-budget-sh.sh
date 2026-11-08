#!/usr/bin/env bash
# scripts/budget.sh against stubs: a fake Actual server behind a stub curl, a
# stub docker that plays actual-admin.mjs, and stub uname/id/open/pbcopy. No
# real Actual server, container or budget is touched.
#
#   bash scripts/test-budget-sh.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
budget="${ROOT}/scripts/budget.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

export ENV_FILE="$tmp/.env"
export BUDGET_ENV_PATH="$tmp/actual/budget.env"
export STUB="$tmp/stub"
mkdir -p "$tmp/bin" "$tmp/actual" "$STUB"
f="$BUDGET_ENV_PATH"

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

# --- Stubs ---

# A fake Actual server. State in $STUB: down (no answer), server_pw (set once
# bootstrapped), files.json (the data array of /sync/list-user-files).
cat > "$tmp/bin/curl" <<'SH'
#!/usr/bin/env bash
url="" stdin=0 hdr=""
prev=""
for a in "$@"; do
  case "$a" in http://* | https://*) url="$a" ;; esac
  [[ "$prev" == --data-binary && "$a" == @- ]] && stdin=1
  [[ "$prev" == -H && "$a" == @* ]] && hdr="${a#@}"
  prev="$a"
done
printf '%s\n' "$*" >> "$STUB/curl.argv"
[[ -e "$STUB/down" ]] && exit 7
body=""
[[ "$stdin" == 1 ]] && body="$(cat)"
field() { printf '%s' "$body" | sed -n "s/.*\"$1\":\"\\([^\"]*\\)\".*/\\1/p"; }
authed() { [[ -n "$hdr" ]] && grep -q '^X-ACTUAL-TOKEN: tok-1' "$hdr"; }
case "$url" in
  */account/needs-bootstrap)
    if [[ -e "$STUB/server_pw" ]]; then b=true; else b=false; fi
    printf '{"status":"ok","data":{"bootstrapped":%s,"loginMethod":"password"}}' "$b" ;;
  */account/bootstrap)
    if [[ -e "$STUB/server_pw" ]]; then
      printf '{"status":"error","reason":"already-bootstrapped"}'
    else
      field password > "$STUB/server_pw"
      printf '{"status":"ok","data":{"token":"tok-1"}}'
    fi ;;
  */account/login)
    if [[ -e "$STUB/server_pw" && "$(field password)" == "$(cat "$STUB/server_pw")" ]]; then
      printf '{"status":"ok","data":{"token":"tok-1"}}'
    else
      printf '{"status":"error","reason":"invalid-password"}'
    fi ;;
  */account/change-password)
    if ! authed; then printf '{"status":"error","reason":"unauthorized"}'
    elif [[ "$(field currentPassword)" != "$(cat "$STUB/server_pw")" ]]; then
      printf '{"status":"error","reason":"invalid-current-password"}'
    else
      field password > "$STUB/server_pw"
      printf '{"status":"ok","data":{}}'
    fi ;;
  */sync/list-user-files)
    if authed; then
      printf '{"status":"ok","data":[%s]}' "$(cat "$STUB/files.json" 2>/dev/null)"
    else
      printf '{"status":"error","reason":"unauthorized"}'
    fi ;;
  *) exit 22 ;;
esac
SH

# docker: compose ps lists nothing; compose run ... actual-admin.mjs plays the
# admin script against the same state (enc_pw marks an encrypted budget,
# list.tsv is what list prints).
cat > "$tmp/bin/docker" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB/docker.argv"
sub="" next=0
for a in "$@"; do
  [[ "$next" == 1 ]] && { sub="$a"; break; }
  [[ "$a" == /opt/actual-admin.mjs ]] && next=1
done
[[ -n "$sub" ]] || exit 0
printf 'run %s ACTUAL_SERVER_URL=%s ACTUAL_BUDGET_FILE=%s\n' "$sub" "${ACTUAL_SERVER_URL:-}" "${ACTUAL_BUDGET_FILE:-}" >> "$STUB/admin.log"
if [[ -e "$STUB/timeout" ]]; then echo timeout; exit 4; fi
if [[ -n "${ACTUAL_PASSWORD:-}" && -e "$STUB/server_pw" && "$ACTUAL_PASSWORD" != "$(cat "$STUB/server_pw")" ]]; then
  echo login; exit 3
fi
case "$sub" in
  check)
    if [[ -e "$STUB/enc_pw" && "${ACTUAL_BUDGET_ENCRYPTION_PASSWORD:-}" != "$(cat "$STUB/enc_pw")" ]]; then
      echo encryption; exit 2
    fi
    [[ -n "${ACTUAL_BUDGET_SYNC_ID:-}" ]] || exit 1 ;;
  create) printf 'sync-new\n' ;;
  list) cat "$STUB/list.tsv" 2>/dev/null ;;
  *) exit 1 ;;
esac
SH

cat > "$tmp/bin/uname" <<'SH'
#!/bin/sh
echo "${STUB_UNAME:-Darwin}"
SH
cat > "$tmp/bin/id" <<'SH'
#!/bin/sh
if [ "${1:-}" = -u ]; then echo "${STUB_UID:-501}"; else exec /usr/bin/id "$@"; fi
SH
cat > "$tmp/bin/open" <<'SH'
#!/bin/sh
printf '%s\n' "$1" > "$STUB/opened"
SH
cat > "$tmp/bin/pbcopy" <<'SH'
#!/bin/sh
cat > "$STUB/clip"
SH
chmod +x "$tmp/bin/"*
export PATH="$tmp/bin:$PATH"

out="$tmp/out"
all="$tmp/all"
: > "$all"
run() {
  local rc=0
  "$budget" "$@" < /dev/null > "$out" 2>&1 || rc=$?
  cat "$out" >> "$all"
  return "$rc"
}
reset() {
  rm -f "$f"
  (cd "$STUB" && rm -f server_pw files.json enc_pw list.tsv down admin.log opened clip)
  printf 'COMPOSE_PROFILES=core\nACTUAL_MCP_ENABLED=0\nACTUAL_LOCAL_ENABLED=0\nACTUAL_BUDGET_FILE=\n' > "$ENV_FILE"
}
mode() { stat -f %Lp "$1" 2>/dev/null || stat -c %a "$1"; }
budget_entry() { # groupId name [deleted]
  printf '{"deleted":%s,"fileId":"file-%s","groupId":"%s","name":"%s","encryptKeyId":null,"owner":null,"usersWithAccess":[{"userId":"u","userName":"","displayName":"","owner":1}]}' \
    "${3:-0}" "$1" "$1" "$2"
}

# --- add ---
reset
printf 'right-SECRET' > "$STUB/server_pw"

# A refused server password: nothing written, and the error says which password.
if ACTUAL_PASSWORD=wrong-SECRET ACTUAL_BUDGET_SYNC_ID=sync-1 ACTUAL_BUDGET_ENCRYPTION_PASSWORD='' \
  run add --url https://budget.example.com; then
  fail "add with a refused server password must fail"
fi
grep -q 'server password' "$out" || fail "the error names the server password ($(cat "$out"))"
[ ! -e "$f" ] || fail "a refused server password writes nothing"
grep -q 'ACTUAL_MCP_ENABLED=0' "$ENV_FILE" || fail "a failed add leaves .env alone"

# The credentials reach the container as environment, not argv.
grep -q -- '-e ACTUAL_PASSWORD' "$STUB/docker.argv" || fail "docker run passes ACTUAL_PASSWORD by name"
grep -q -- '--entrypoint node' "$STUB/docker.argv" || fail "docker run overrides the entrypoint with node"
grep -q 'ACTUAL_SERVER_URL=https://budget.example.com ' "$STUB/admin.log" || fail "the check uses the given URL"
grep -q 'ACTUAL_BUDGET_FILE=./compose/actual/no-budget.env' "$STUB/admin.log" \
  || fail "admin runs mount no-budget.env, not the real budget file"

# A wrong or missing encryption password: nothing written, and the error names it.
printf 'enc-SECRET' > "$STUB/enc_pw"
if ACTUAL_PASSWORD=right-SECRET ACTUAL_BUDGET_SYNC_ID=sync-1 ACTUAL_BUDGET_ENCRYPTION_PASSWORD=bad-enc-SECRET \
  run add --url https://budget.example.com; then
  fail "add with a wrong encryption password must fail"
fi
grep -q 'encryption password' "$out" || fail "the error names the encryption password ($(cat "$out"))"
[ ! -e "$f" ] || fail "a wrong encryption password writes nothing"

# Success: mode 600, the five keys, and the flags in .env.
ACTUAL_PASSWORD=right-SECRET ACTUAL_BUDGET_SYNC_ID=sync-1 ACTUAL_BUDGET_ENCRYPTION_PASSWORD=enc-SECRET \
  run add --url https://budget.example.com/ || fail "add should succeed ($(cat "$out"))"
[ -f "$f" ] || fail "add writes budget.env"
[ "$(mode "$f")" = 600 ] || fail "budget.env is mode 600"
grep -qx 'ACTUAL_SERVER_URL=https://budget.example.com' "$f" || fail "add stores the URL without a trailing slash"
grep -qx 'ACTUAL_WEB_URL=https://budget.example.com/' "$f" || fail "add stores the web URL"
grep -qx 'ACTUAL_PASSWORD=right-SECRET' "$f" || fail "add stores the server password"
grep -qx 'ACTUAL_BUDGET_SYNC_ID=sync-1' "$f" || fail "add stores the sync id"
grep -qx 'ACTUAL_BUDGET_ENCRYPTION_PASSWORD=enc-SECRET' "$f" || fail "add stores the encryption password"
[ "$(grep -c '^ACTUAL_' "$f")" = 5 ] || fail "budget.env has five keys"
grep -qx 'ACTUAL_MCP_ENABLED=1' "$ENV_FILE" || fail "add sets ACTUAL_MCP_ENABLED=1"
grep -qx "ACTUAL_BUDGET_FILE=${f}" "$ENV_FILE" || fail "add points ACTUAL_BUDGET_FILE at the file"
grep -qx 'ACTUAL_LOCAL_ENABLED=0' "$ENV_FILE" || fail "add leaves ACTUAL_LOCAL_ENABLED alone"

# An existing file needs --force.
if ACTUAL_PASSWORD=right-SECRET ACTUAL_BUDGET_SYNC_ID=sync-2 ACTUAL_BUDGET_ENCRYPTION_PASSWORD=enc-SECRET \
  run add --url https://budget.example.com; then
  fail "add must not overwrite without --force"
fi
grep -q -- '--force' "$out" || fail "the refusal mentions --force"
grep -qx 'ACTUAL_BUDGET_SYNC_ID=sync-1' "$f" || fail "a refused add leaves the file alone"

# --force on an unencrypted budget: no encryption line.
rm -f "$STUB/enc_pw"
ACTUAL_PASSWORD=right-SECRET ACTUAL_BUDGET_SYNC_ID=sync-2 ACTUAL_BUDGET_ENCRYPTION_PASSWORD='' \
  run add --url https://budget.example.com --force || fail "--force overwrites ($(cat "$out"))"
grep -qx 'ACTUAL_BUDGET_SYNC_ID=sync-2' "$f" || fail "--force writes the new sync id"
if grep -q '^ACTUAL_BUDGET_ENCRYPTION_PASSWORD' "$f"; then fail "no encryption password, no line"; fi
[ "$(mode "$f")" = 600 ] || fail "an overwritten budget.env stays mode 600"

# No sync id: adopt the only budget, refuse to guess between two.
rm -f "$f"
printf 'sync-only\tHousehold\n' > "$STUB/list.tsv"
ACTUAL_PASSWORD=right-SECRET ACTUAL_BUDGET_ENCRYPTION_PASSWORD='' run add --url https://budget.example.com \
  || fail "add adopts the only budget ($(cat "$out"))"
grep -qx 'ACTUAL_BUDGET_SYNC_ID=sync-only' "$f" || fail "add stores the adopted sync id"
rm -f "$f"
printf 'sync-a\tHousehold\nsync-b\tBusiness\n' > "$STUB/list.tsv"
if ACTUAL_PASSWORD=right-SECRET ACTUAL_BUDGET_ENCRYPTION_PASSWORD='' run add --url https://budget.example.com; then
  fail "add with two budgets and no sync id must fail"
fi
{ grep -q 'Household' "$out" && grep -q 'sync-b' "$out"; } || fail "add lists the budgets to choose from"
[ ! -e "$f" ] || fail "an ambiguous add writes nothing"

# The server never answers: actual-admin.mjs exits 4, and nothing is written.
rm -f "$f"
cp "$ENV_FILE" "$tmp/env-before"
touch "$STUB/timeout"
if ACTUAL_PASSWORD=right-SECRET ACTUAL_BUDGET_SYNC_ID=sync-1 ACTUAL_BUDGET_ENCRYPTION_PASSWORD='' \
  run add --url https://budget.example.com; then
  fail "add must fail when the check times out"
fi
grep -q 'no answer from https://budget.example.com within 45 s' "$out" || fail "a timeout names the server ($(cat "$out"))"
[ ! -e "$f" ] || fail "a timed-out add writes nothing"
cmp -s "$ENV_FILE" "$tmp/env-before" || fail "a timed-out add leaves .env alone"
if ACTUAL_PASSWORD=right-SECRET ACTUAL_BUDGET_ENCRYPTION_PASSWORD='' run add --url https://budget.example.com; then
  fail "add must fail when list times out"
fi
grep -q 'no answer from' "$out" || fail "a list timeout says so"
ACTUAL_ADMIN_TIMEOUT_S=7 ACTUAL_PASSWORD=right-SECRET ACTUAL_BUDGET_SYNC_ID=sync-1 ACTUAL_BUDGET_ENCRYPTION_PASSWORD='' \
  run add --url https://u:url-SECRET@budget.example.com && fail "add must fail on a timeout"
grep -q 'within 7 s' "$out" || fail "the timeout message uses ACTUAL_ADMIN_TIMEOUT_S"
grep -q 'https://\*\*\*@budget.example.com' "$out" || fail "the timeout message masks user:password@ in the URL"
rm -f "$STUB/timeout"
grep -q -- '-e ACTUAL_ADMIN_TIMEOUT_S' "$STUB/docker.argv" || fail "docker run passes ACTUAL_ADMIN_TIMEOUT_S by name"

# Usage errors.
run add && fail "add needs --url"
ACTUAL_PASSWORD=x ACTUAL_BUDGET_SYNC_ID=y run add --url budget.example.com && fail "URL without http(s) accepted"
run add --url https://budget.example.com && fail "no password and no terminal must fail"
[ ! -e "$f" ] || fail "a failed add writes nothing"

# Linux with a uid other than 1000: the container couldn't read the file.
if STUB_UNAME=Linux STUB_UID=1001 ACTUAL_PASSWORD=right-SECRET ACTUAL_BUDGET_SYNC_ID=sync-1 \
  run add --url https://budget.example.com; then
  fail "add on Linux as uid 1001 must refuse"
fi
grep -q '1000' "$out" || fail "the refusal says the container reads the file as uid 1000"
[ ! -e "$f" ] || fail "a refused add writes nothing"
STUB_UNAME=Linux STUB_UID=1000 ACTUAL_PASSWORD=right-SECRET ACTUAL_BUDGET_SYNC_ID=sync-1 ACTUAL_BUDGET_ENCRYPTION_PASSWORD='' \
  run add --url https://budget.example.com || fail "add on Linux as uid 1000 ($(cat "$out"))"

# Passwords with quotes. dotenv has no escapes, so budget.env picks a quote the
# value doesn't contain, and refuses a value no encoding can carry.
rm -f "$f"
quoted="q'u\"o te-SECRET"
printf '%s' "$quoted" > "$STUB/server_pw"
ACTUAL_PASSWORD="$quoted" ACTUAL_BUDGET_SYNC_ID=sync-1 ACTUAL_BUDGET_ENCRYPTION_PASSWORD='' \
  run add --url https://budget.example.com || fail "add with ' and \" in the password ($(cat "$out"))"
grep -qxF "ACTUAL_PASSWORD=\`${quoted}\`" "$f" || fail "a password with ' and \" is stored in backticks"
run open || fail "open with a quoted password ($(cat "$out"))"
[ "$(cat "$STUB/clip")" = "$quoted" ] || fail "the quoted password reads back unchanged"
rm -f "$f"
cp "$ENV_FILE" "$tmp/env-before"
unstorable="a'b\"c\`d#e-SECRET"
printf '%s' "$unstorable" > "$STUB/server_pw"
if ACTUAL_PASSWORD="$unstorable" ACTUAL_BUDGET_SYNC_ID=sync-1 ACTUAL_BUDGET_ENCRYPTION_PASSWORD='' \
  run add --url https://budget.example.com; then
  fail "a password budget.env can't hold must fail"
fi
grep -q 'ACTUAL_PASSWORD' "$out" || fail "the refusal names ACTUAL_PASSWORD ($(cat "$out"))"
[ ! -e "$f" ] || fail "an unstorable password writes no budget.env"
cmp -s "$ENV_FILE" "$tmp/env-before" || fail "an unstorable password leaves .env alone"
if ACTUAL_PASSWORD=right-SECRET ACTUAL_BUDGET_SYNC_ID=sync-1 ACTUAL_BUDGET_ENCRYPTION_PASSWORD="$(printf 'two\nline-SECRET')" \
  run add --url https://budget.example.com; then
  fail "an unstorable encryption password must fail"
fi
grep -q 'ACTUAL_BUDGET_ENCRYPTION_PASSWORD' "$out" || fail "the refusal names the encryption password"
[ ! -e "$f" ] || fail "an unstorable encryption password writes nothing"
rm -f "$STUB/server_pw"

# --- init ---
# A new server with one budget: bootstrap with a generated password, adopt it.
reset
budget_entry sync-local "My Budget" > "$STUB/files.json"
run init || fail "init on a new server ($(cat "$out"))"
[ -s "$STUB/server_pw" ] || fail "init bootstraps the server"
pw="$(cat "$STUB/server_pw")"
[ "${#pw}" = 48 ] || fail "init generates a 48-character password"
grep -qx "ACTUAL_PASSWORD=${pw}" "$f" || fail "init stores the generated password"
grep -qx 'ACTUAL_SERVER_URL=http://actual-server:5006' "$f" || fail "init points at actual-server"
grep -qx 'ACTUAL_WEB_URL=http://localhost:5006/' "$f" || fail "init stores the local web URL"
grep -qx 'ACTUAL_BUDGET_SYNC_ID=sync-local' "$f" || fail "init adopts the only budget"
[ "$(mode "$f")" = 600 ] || fail "init writes budget.env mode 600"
grep -qx 'ACTUAL_MCP_ENABLED=1' "$ENV_FILE" || fail "init sets ACTUAL_MCP_ENABLED=1"
grep -qx 'ACTUAL_LOCAL_ENABLED=1' "$ENV_FILE" || fail "init sets ACTUAL_LOCAL_ENABLED=1"
grep -qx "ACTUAL_BUDGET_FILE=${f}" "$ENV_FILE" || fail "init points ACTUAL_BUDGET_FILE at the file"
grep -q 'create' "$STUB/admin.log" && fail "init with one budget creates none"
[ ! -e "$tmp/actual/server-password.pending" ] || fail "init leaves no pending password file"
if grep -q "$pw" "$STUB/curl.argv"; then fail "the password went to curl's argv"; fi
grep -q 'ACTUAL_SERVER_URL=http://actual-server:5006 ' "$STUB/admin.log" || fail "init checks through actual-server"
pws="$pw"

# Re-running init with the file in place changes nothing.
cp "$f" "$tmp/before"
run init || fail "init again ($(cat "$out"))"
cmp -s "$f" "$tmp/before" || fail "init again leaves budget.env alone"

# --rotate: a new password on the server and in the file.
run init --rotate || fail "init --rotate ($(cat "$out"))"
new="$(cat "$STUB/server_pw")"
[ "$new" != "$pw" ] && [ "${#new}" = 48 ] || fail "--rotate sets a new generated password"
grep -qx "ACTUAL_PASSWORD=${new}" "$f" || fail "--rotate stores the new password"
grep -qx 'ACTUAL_BUDGET_SYNC_ID=sync-local' "$f" || fail "--rotate keeps the sync id"
[ "$(mode "$f")" = 600 ] || fail "--rotate keeps mode 600"
pws="$pws $new"

# A new server with no budget: init creates one.
reset
run init || fail "init with no budget ($(cat "$out"))"
grep -q '^run create' "$STUB/admin.log" || fail "init creates a budget"
grep -qx 'ACTUAL_BUDGET_SYNC_ID=sync-new' "$f" || fail "init stores the created budget's sync id"
pws="$pws $(cat "$STUB/server_pw")"

# Two budgets: init lists them and needs SYNC_ID. Deleted budgets don't count.
reset
printf 'srv-SECRET' > "$STUB/server_pw"
{ budget_entry sync-a Household; printf ','; budget_entry sync-b Business; printf ','; budget_entry sync-gone Old 1; } > "$STUB/files.json"
if ACTUAL_PASSWORD=srv-SECRET run init; then fail "init with two budgets and no SYNC_ID must fail"; fi
{ grep -q 'Household' "$out" && grep -q 'Business' "$out"; } || fail "init lists the budgets ($(cat "$out"))"
grep -q 'Old' "$out" && fail "init leaves out deleted budgets"
grep -q 'SYNC_ID' "$out" || fail "init says to pass SYNC_ID"
[ ! -e "$f" ] || fail "an ambiguous init writes nothing"
grep -qx 'ACTUAL_MCP_ENABLED=0' "$ENV_FILE" || fail "a failed init leaves .env alone"
ACTUAL_PASSWORD=srv-SECRET ACTUAL_BUDGET_SYNC_ID=sync-b run init || fail "init with SYNC_ID ($(cat "$out"))"
grep -qx 'ACTUAL_BUDGET_SYNC_ID=sync-b' "$f" || fail "init stores the chosen sync id"
grep -qx 'ACTUAL_PASSWORD=srv-SECRET' "$f" || fail "init stores the given password for a bootstrapped server"
rm -f "$f"
if ACTUAL_PASSWORD=srv-SECRET ACTUAL_BUDGET_SYNC_ID=sync-gone run init; then fail "init with a deleted budget's sync id must fail"; fi

# A bootstrapped server and no password: no terminal to ask in.
if run init; then fail "init on a bootstrapped server with no password must fail"; fi
grep -q 'ACTUAL_PASSWORD' "$out" || fail "the error says to set ACTUAL_PASSWORD"
if ACTUAL_PASSWORD=wrong-SECRET ACTUAL_BUDGET_SYNC_ID=sync-b run init; then fail "init with a refused password must fail"; fi
grep -q 'server password' "$out" || fail "a refused init password names the server password"
[ ! -e "$f" ] || fail "a failed init writes nothing"

# The server isn't up.
touch "$STUB/down"
if run init; then fail "init with no server must fail"; fi
grep -q 'make up' "$out" || fail "the error says to start the server"
rm -f "$STUB/down"

if STUB_UNAME=Linux STUB_UID=1001 ACTUAL_PASSWORD=srv-SECRET ACTUAL_BUDGET_SYNC_ID=sync-b run init; then
  fail "init on Linux as uid 1001 must refuse"
fi
grep -q '1000' "$out" || fail "init's refusal names uid 1000"
# A create that times out: init fails and writes nothing.
reset
touch "$STUB/timeout"
run init && fail "init must fail when create times out"
grep -q 'no answer from' "$out" || fail "init says the create timed out ($(cat "$out"))"
[ ! -e "$f" ] || fail "a timed-out init writes no budget.env"
grep -qx 'ACTUAL_MCP_ENABLED=0' "$ENV_FILE" || fail "a timed-out init leaves .env alone"
rm -f "$STUB/timeout"
pws="$pws $(cat "$STUB/server_pw")"

run init --frob && fail "init rejects unknown options"
rm -f "$f"
run init --rotate && fail "init --rotate with no budget file must fail"
grep -q 'make budget-init' "$out" || fail "--rotate with no file says to run make budget-init ($(cat "$out"))"

# --- check (make doctor runs it) ---
reset
printf 'check-SECRET' > "$STUB/server_pw"
printf 'ACTUAL_SERVER_URL=http://actual-server:5006\nACTUAL_PASSWORD=check-SECRET\nACTUAL_BUDGET_SYNC_ID=sync-c\n' > "$f"
chmod 600 "$f"
run check || fail "check on a good file ($(cat "$out"))"
printf 'other' > "$STUB/server_pw"
rc=0; run check || rc=$?
[ "$rc" = 3 ] || fail "check exits 3 when the server refuses the password (got $rc)"
printf 'check-SECRET' > "$STUB/server_pw"
printf 'enc-SECRET' > "$STUB/enc_pw"
rc=0; run check || rc=$?
[ "$rc" = 2 ] || fail "check exits 2 for a missing encryption password (got $rc)"
rm -f "$STUB/enc_pw"
touch "$STUB/timeout"
rc=0; run check || rc=$?
[ "$rc" = 4 ] || fail "check exits 4 on a timeout (got $rc)"
rm -f "$STUB/timeout"
rm -f "$f" "$STUB/enc_pw"
rc=0; run check || rc=$?
[ "$rc" = 1 ] || fail "check with no file exits 1 (got $rc)"

# --- access ---
reset
cp "$ENV_FILE" "$tmp/env-before"
run access maybe && fail "access maybe must fail"
cmp -s "$ENV_FILE" "$tmp/env-before" || fail "a refused access leaves .env alone"
run access && fail "access needs read or write"
run access read || fail "access read ($(cat "$out"))"
grep -qx 'ACTUAL_ACCESS=read' "$ENV_FILE" || fail "access read sets ACTUAL_ACCESS=read"
run access write || fail "access write"
[ "$(grep -c '^ACTUAL_ACCESS=' "$ENV_FILE")" = 1 ] || fail "access replaces the line"
grep -qx 'ACTUAL_ACCESS=write' "$ENV_FILE" || fail "access write sets ACTUAL_ACCESS=write"
grep -q 'hermes-reload' "$out" || fail "access says to run make hermes-reload"

# --- open ---
reset
run open && fail "open with no budget file must fail"
printf 'ACTUAL_SERVER_URL=http://actual-server:5006\nACTUAL_WEB_URL=http://localhost:5006/\nACTUAL_PASSWORD=open-SECRET\nACTUAL_BUDGET_SYNC_ID=s\n' > "$f"
chmod 600 "$f"
run open || fail "open ($(cat "$out"))"
grep -qx 'http://localhost:5006/' "$STUB/opened" || fail "open opens ACTUAL_WEB_URL"
[ "$(cat "$STUB/clip")" = open-SECRET ] || fail "open copies the server password"
grep -q 'clipboard' "$out" || fail "open says the password is on the clipboard"

# --- remove ---
printf 'ACTUAL_MCP_ENABLED=1\nACTUAL_LOCAL_ENABLED=1\nACTUAL_BUDGET_FILE=%s\n' "$f" > "$ENV_FILE"
run remove || fail "remove ($(cat "$out"))"
[ ! -e "$f" ] || fail "remove deletes budget.env"
grep -qx 'ACTUAL_MCP_ENABLED=0' "$ENV_FILE" || fail "remove sets ACTUAL_MCP_ENABLED=0"
grep -qx 'ACTUAL_BUDGET_FILE=' "$ENV_FILE" || fail "remove clears ACTUAL_BUDGET_FILE"
grep -qx 'ACTUAL_LOCAL_ENABLED=1' "$ENV_FILE" || fail "remove leaves the local server on"
run remove && fail "remove with no budget file must fail"

run frobnicate && fail "unknown subcommand must fail"

# --- No password in any output, or in curl or docker argv ---
for s in right-SECRET wrong-SECRET enc-SECRET bad-enc-SECRET srv-SECRET open-SECRET check-SECRET url-SECRET "$quoted" "$unstorable" line-SECRET $pws; do
  for log in "$all" "$STUB/curl.argv" "$STUB/docker.argv"; do
    if grep -qF -- "$s" "$log"; then fail "${log##*/} contains a password"; fi
  done
done

printf 'OK budget.sh\n'
