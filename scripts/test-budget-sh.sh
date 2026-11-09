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
[[ -e "$STUB/curl_timeout" ]] && exit 28
[[ -e "$STUB/html" ]] && { printf '<html><body>Welcome</body></html>'; exit 0; }
body=""
[[ "$stdin" == 1 ]] && body="$(cat)"
field() { printf '%s' "$body" | sed -n "s/.*\"$1\":\"\\([^\"]*\\)\".*/\\1/p"; }
# The login password, JSON-decoded (it may hold escapes).
login_pw() { printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"], end="")'; }
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
    if [[ -e "$STUB/server_pw" && "$(login_pw)" == "$(cat "$STUB/server_pw")" ]]; then
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
if [[ -e "$STUB/admin_rc" ]]; then echo "stub exit"; exit "$(cat "$STUB/admin_rc")"; fi
if [[ -n "${ACTUAL_SESSION_TOKEN:-}" && "$ACTUAL_SESSION_TOKEN" != tok-1 ]]; then echo login; exit 3; fi
printf 'token=%s sync=%s\n' "${ACTUAL_SESSION_TOKEN:-}" "${ACTUAL_BUDGET_SYNC_ID:-}" >> "$STUB/admin.env"
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
  (cd "$STUB" && rm -f server_pw files.json enc_pw list.tsv down html curl_timeout admin_rc admin.env admin.log opened clip)
  printf 'COMPOSE_PROFILES=core\nACTUAL_MCP_ENABLED=0\nACTUAL_LOCAL_ENABLED=0\nACTUAL_BUDGET_FILE=\n' > "$ENV_FILE"
}
mode() { stat -f %Lp "$1" 2>/dev/null || stat -c %a "$1"; }
budget_entry() { # groupId name [deleted] [encryptKeyId]
  local key=null
  [[ -n "${4:-}" ]] && key="\"$4\""
  printf '{"deleted":%s,"fileId":"file-%s","groupId":"%s","name":"%s","encryptKeyId":'"$key"',"owner":null,"usersWithAccess":[{"userId":"u","userName":"","displayName":"","owner":1}]}' \
    "${3:-0}" "$1" "$1" "$2"
}
# budgets_run INPUT: budget.sh budgets --json with INPUT on stdin; stdout in
# $tmp/jout, stderr in $tmp/jerr, exit code in $brc.
brc=0
budgets_run() {
  brc=0
  printf '%s' "$1" | STACK_PYTHON=host BUDGET_DATA_ROOT="$tmp/data" "$budget" budgets --json > "$tmp/jout" 2> "$tmp/jerr" || brc=$?
  cat "$tmp/jout" "$tmp/jerr" >> "$all"
}
jlines_ok() { python3 -c 'import json,sys; [json.loads(l) for l in sys.stdin]' < "$tmp/jout"; }
jget() { # LINE FIELD
  sed -n "${1}p" "$tmp/jout" | python3 -c 'import json,sys; v=json.load(sys.stdin)["'"$2"'"]; print(json.dumps(v) if isinstance(v,bool) else v)'
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
{ [ "$new" != "$pw" ] && [ "${#new}" = 48 ]; } || fail "--rotate sets a new generated password"
grep -qx "ACTUAL_PASSWORD=${new}" "$f" || fail "--rotate stores the new password"
grep -qx 'ACTUAL_BUDGET_SYNC_ID=sync-local' "$f" || fail "--rotate keeps the sync id"
[ "$(mode "$f")" = 600 ] || fail "--rotate keeps mode 600"
pws="$pws $new"
mkdir -p "$tmp/rot/addons/actual/secrets"
printf '%s' "$pw" > "$tmp/rot/addons/actual/secrets/server_password"
BUDGET_DATA_ROOT="$tmp/rot" run init --rotate || fail "init --rotate with a stored copy ($(cat "$out"))"
new2="$(cat "$STUB/server_pw")"
[ "$(cat "$tmp/rot/addons/actual/secrets/server_password")" = "$new2" ] || fail "--rotate updates secrets/server_password when it exists"
pws="$pws $new2"

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
printf 'ACTUAL_SERVER_URL=https://u:url-SECRET@budget.example.com\nACTUAL_PASSWORD=check-SECRET\nACTUAL_BUDGET_SYNC_ID=\n' > "$f"
rc=0; run check || rc=$?
[ "$rc" = 1 ] || fail "check exits 1 when the budget doesn't open (got $rc)"
grep -q 'on https://\*\*\*@budget.example.com' "$out" || fail "check masks user:password@ in the URL ($(cat "$out"))"
rm -f "$f"
printf 'sync-a\tHousehold\nsync-b\tBusiness\n' > "$STUB/list.tsv"
ACTUAL_PASSWORD=check-SECRET ACTUAL_BUDGET_ENCRYPTION_PASSWORD='' run add --url https://u:url-SECRET@budget.example.com \
  && fail "add with two budgets must fail"
grep -q '^\[budget\] ERROR: https://\*\*\*@budget.example.com has 2 budgets' "$out" || fail "add masks user:password@ in the URL ($(cat "$out"))"
rm -f "$STUB/list.tsv"
rm -f "$f" "$STUB/enc_pw"
rc=0; run check || rc=$?
[ "$rc" = 1 ] || fail "check with no file exits 1 (got $rc)"

# --- budgets ---
reset
printf 'right-SECRET' > "$STUB/server_pw"
{ budget_entry sync-a 'Fam \"q\" \\ x\ty é'; printf ','; budget_entry sync-b Business 0 key-1; printf ','; budget_entry sync-gone Old 1; } > "$STUB/files.json"

budgets_run '{"server":"https://budget.example.com","password":"right-SECRET"}'
[ "$brc" = 0 ] || fail "budgets with the right password exits 0 (got $brc: $(cat "$tmp/jout" "$tmp/jerr"))"
jlines_ok || fail "every budgets line is JSON ($(cat "$tmp/jout"))"
[ "$(wc -l < "$tmp/jout" | tr -d ' ')" = 3 ] || fail "two budget lines and done ($(cat "$tmp/jout"))"
[ "$(jget 1 phase)" = budget ] || fail "first line is a budget"
[ "$(jget 1 sync_id)" = sync-a ] || fail "sync_id"
[ "$(jget 1 name)" = 'Fam "q" \ x y é' ] || fail "the name round-trips ($(sed -n 1p "$tmp/jout"))"
[ "$(jget 1 encrypted)" = false ] || fail "no encryptKeyId is encrypted:false"
[ "$(jget 2 encrypted)" = true ] || fail "an encryptKeyId is encrypted:true"
[ "$(jget 3 phase)" = "done" ] || fail "the last line is done"
[ ! -s "$tmp/jerr" ] || fail "a good run writes nothing to stderr ($(cat "$tmp/jerr"))"

budgets_run '{"server":"https://budget.example.com/","password":"wrong-SECRET"}'
[ "$brc" = 4 ] || fail "a wrong password exits 4 (got $brc)"
jlines_ok || fail "the error line is JSON"
[ "$(wc -l < "$tmp/jout" | tr -d ' ')" = 1 ] || fail "a wrong password gives one line"
[ "$(jget 1 error)" = login_refused ] || fail "a wrong password is login_refused ($(cat "$tmp/jout"))"
grep -q '^error: ' "$tmp/jerr" || fail "the error text also goes to stderr"

touch "$STUB/html"
budgets_run '{"server":"https://budget.example.com","password":"right-SECRET"}'
[ "$brc" = 4 ] && [ "$(jget 1 error)" = server_unreachable ] || fail "an <html> answer is server_unreachable ($(cat "$tmp/jout"))"
rm -f "$STUB/html"

touch "$STUB/down"
budgets_run '{"server":"https://budget.example.com","password":"right-SECRET"}'
[ "$brc" = 4 ] && [ "$(jget 1 error)" = server_unreachable ] || fail "a server that is down is server_unreachable ($(cat "$tmp/jout"))"
rm -f "$STUB/down"

touch "$STUB/curl_timeout"
budgets_run '{"server":"https://budget.example.com","password":"right-SECRET"}'
[ "$brc" = 4 ] && [ "$(jget 1 error)" = timeout ] || fail "curl's timeout is timeout ($(cat "$tmp/jout"))"
rm -f "$STUB/curl_timeout"
grep -q -- '-m 45' "$STUB/curl.argv" || fail "the budgets calls wait 45 s"

# The local server signs in with the stored server password.
mkdir -p "$tmp/data/addons/actual/secrets"
printf 'right-SECRET' > "$tmp/data/addons/actual/secrets/server_password"
budgets_run '{"server":"local"}'
[ "$brc" = 0 ] && [ "$(jget 1 sync_id)" = sync-a ] || fail "local uses the stored server password ($(cat "$tmp/jout" "$tmp/jerr"))"
grep -q 'http://127.0.0.1:5006/account/login' "$STUB/curl.argv" || fail "local talks to 127.0.0.1:5006"
rm "$tmp/data/addons/actual/secrets/server_password"
budgets_run '{"server":"local"}'
[ "$brc" = 2 ] && [ "$(jget 1 error)" = not_found ] || fail "local without a stored password is not_found ($(cat "$tmp/jout"))"

# A password with a tab, a newline and a quote still signs in.
printf 'tab\there\nnl "q" \\ SECRET' > "$STUB/server_pw"
budgets_run '{"server":"https://budget.example.com","password":"tab\there\nnl \"q\" \\ SECRET"}'
[ "$brc" = 0 ] && [ "$(jget 3 phase)" = "done" ] || fail "a password with control characters signs in ($(cat "$tmp/jout" "$tmp/jerr"))"
printf 'right-SECRET' > "$STUB/server_pw"

# A session token skips the login; a stale one is token_expired.
budgets_run '{"server":"https://budget.example.com","session_token":"tok-1"}'
[ "$brc" = 0 ] && [ "$(jget 3 phase)" = "done" ] || fail "a session token lists the budgets ($(cat "$tmp/jout" "$tmp/jerr"))"
budgets_run '{"server":"https://budget.example.com","session_token":"tok-old"}'
[ "$brc" = 4 ] && [ "$(jget 1 error)" = token_expired ] || fail "a stale token is token_expired ($(cat "$tmp/jout"))"

budgets_run '{"server":"ftp://x","password":"right-SECRET"}'
[ "$brc" = 4 ] && [ "$(jget 1 error)" = server_unreachable ] || fail "a non-http server is an error ($(cat "$tmp/jout"))"
budgets_run 'not json'
[ "$brc" = 2 ] && [ "$(jget 1 phase)" = error ] || fail "bad stdin is an error line ($(cat "$tmp/jout"))"
grep -q 'budgets' <("$budget" --help) || fail "usage mentions budgets"

# --- connect ---
sec="$tmp/data/addons/actual/secrets"
mkdir -p "$sec"
rm -f "$sec"/main.* "$tmp/data/addons/actual/secrets/server_password"
reset
printf 'right-SECRET' > "$STUB/server_pw"
files_json="$(budget_entry sync-a Main)"
printf '%s' "$files_json" > "$STUB/files.json"
connect_run() { # SLUG INPUT
  brc=0
  printf '%s' "$2" | STACK_PYTHON=host BUDGET_DATA_ROOT="$tmp/data" "$budget" connect --slug "$1" --json > "$tmp/jout" 2> "$tmp/jerr" || brc=$?
  cat "$tmp/jout" "$tmp/jerr" >> "$all"
}
nofiles() { ! ls "$sec"/main.* > /dev/null 2>&1; }
srv='"server":"https://budget.example.com","sync_id":"sync-a"'

connect_run main "{$srv,\"password\":\"right-SECRET\"}"
[ "$brc" = 0 ] || fail "password connect ($(cat "$tmp/jout" "$tmp/jerr"))"
[ "$(cat "$sec/main.session_token")" = tok-1 ] || fail "connect stores the session token"
[ "$(mode "$sec/main.session_token")" = 600 ] || fail "the token file is mode 600"
[ ! -e "$sec/main.encryption_password" ] || fail "no encryption file without a password"
[ "$(jget 1 phase)" = "done" ] && [ "$(jget 1 account)" = main ] || fail "connect done line ($(cat "$tmp/jout"))"
grep -qx '{"phase":"done","account":"main","mcp":\["budget-main-ro"\]}' "$tmp/jout" || fail "read connect lists the -ro server ($(cat "$tmp/jout"))"
grep -q 'token=tok-1 sync=sync-a' "$STUB/admin.env" || fail "check gets the token and sync id"
grep -q 'ACTUAL_SERVER_URL=https://budget.example.com ' "$STUB/admin.log" || fail "check uses the remote URL"
grep -q -- '-e ACTUAL_SESSION_TOKEN' "$STUB/docker.argv" || fail "docker gets the token by name"
! grep -rqF right-SECRET "$tmp/data" || fail "no file holds the password"
rm -f "$sec"/main.*

connect_run main "{$srv,\"password\":\"right-SECRET\",\"access\":\"write\",\"profile\":\"default\",\"encryption_password\":\"enc-SECRET\"}"
[ "$brc" = 0 ] || fail "write connect ($(cat "$tmp/jout" "$tmp/jerr"))"
grep -qx '{"phase":"done","account":"main","mcp":\["budget-main-ro","budget-main"\]}' "$tmp/jout" || fail "write connect lists both servers ($(cat "$tmp/jout"))"
[ "$(cat "$sec/main.encryption_password")" = enc-SECRET ] || fail "connect stores the encryption password"
[ "$(mode "$sec/main.encryption_password")" = 600 ] || fail "the encryption file is mode 600"
rm -f "$sec"/main.*

connect_run main "{$srv,\"session_token\":\"tok-1\"}"
[ "$brc" = 0 ] && [ "$(cat "$sec/main.session_token")" = tok-1 ] || fail "a token connect stores the token as given"
rm -f "$sec"/main.*

# The local server: signed in from the host, checked at the container URL.
printf 'right-SECRET' > "$sec/server_password"
connect_run main '{"server":"local","sync_id":"sync-a"}'
[ "$brc" = 0 ] || fail "local connect ($(cat "$tmp/jout" "$tmp/jerr"))"
tail -1 "$STUB/admin.log" | grep -q 'ACTUAL_SERVER_URL=http://actual-server:5006 ' || fail "local check uses the container URL"
grep -q 'http://127.0.0.1:5006/account/login' "$STUB/curl.argv" || fail "local sign-in uses the host URL"
rm -f "$sec"/main.* "$sec/server_password"

# Each failure names its error and writes nothing.
expect_err() { # LABEL RC NAME
  [ "$brc" = "$2" ] && [ "$(jget 1 error)" = "$3" ] || fail "$1: want $3/$2 ($(cat "$tmp/jout" "$tmp/jerr"))"
  nofiles || fail "$1 leaves secrets empty"
}
pwin="{$srv,\"password\":\"right-SECRET\"}"
printf 'enc-SECRET' > "$STUB/enc_pw"
connect_run main "$pwin"
expect_err "missing encryption password" 4 encryption_password
rm -f "$STUB/enc_pw"
printf '2' > "$STUB/admin_rc"; connect_run main "$pwin"; expect_err "admin 2" 4 encryption_password
printf '3' > "$STUB/admin_rc"; connect_run main "$pwin"; expect_err "admin 3 with a password" 4 login_refused
connect_run main "{$srv,\"session_token\":\"tok-1\"}"; expect_err "admin 3 with a token" 4 token_expired
printf '4' > "$STUB/admin_rc"; connect_run main "$pwin"; expect_err "admin 4" 4 timeout
printf '5' > "$STUB/admin_rc"; connect_run main "$pwin"; expect_err "admin 5" 4 no_access
printf '6' > "$STUB/admin_rc"; connect_run main "$pwin"; expect_err "admin 6" 4 server_unreachable
rm -f "$STUB/admin_rc"
connect_run main "{$srv,\"password\":\"wrong-SECRET\"}"; expect_err "refused password" 4 login_refused
connect_run main "{$srv,\"session_token\":\"tok-old\"}"; expect_err "stale token" 4 token_expired
touch "$STUB/down"; connect_run main "$pwin"; expect_err "server down" 4 server_unreachable
rm -f "$STUB/down"
connect_run Bad_Slug "$pwin"
[ "$brc" = 2 ] && [ "$(jget 1 error)" = bad_slug ] || fail "bad slug"
connect_run 'a
b' "$pwin"
[ "$brc" = 2 ] && [ "$(jget 1 error)" = bad_slug ] || fail "a slug with a newline is bad_slug"
connect_run main "{$srv,\"password\":\"right-SECRET\",\"profile\":\"alice\"}"
[ "$brc" = 2 ] && [ "$(jget 1 error)" = unknown_profile ] || fail "unknown profile"
connect_run main "{$srv,\"password\":\"right-SECRET\",\"access\":\"admin\"}"
[ "$brc" = 2 ] && [ "$(jget 1 error)" = bad_access ] || fail "bad access"
nofiles || fail "input errors leave secrets empty"

# --- disconnect ---
disconnect_run() { # SLUG
  brc=0
  STACK_PYTHON=host BUDGET_DATA_ROOT="$tmp/data" "$budget" disconnect --slug "$1" --json < /dev/null > "$tmp/jout" 2> "$tmp/jerr" || brc=$?
  cat "$tmp/jout" "$tmp/jerr" >> "$all"
}
printf 'tok-1' > "$sec/main.session_token"; printf 'enc-SECRET' > "$sec/main.encryption_password"
printf 'keep' > "$sec/other.session_token"
disconnect_run main
[ "$brc" = 0 ] && [ "$(jget 1 phase)" = "done" ] || fail "disconnect ($(cat "$tmp/jout" "$tmp/jerr"))"
nofiles || fail "disconnect removes both secret files"
[ -e "$sec/other.session_token" ] || fail "disconnect leaves other slugs alone"
disconnect_run nope
[ "$brc" = 2 ] && [ "$(jget 1 error)" = not_found ] || fail "disconnect of an unknown slug is not_found"
disconnect_run Bad_Slug
[ "$brc" = 2 ] && [ "$(jget 1 error)" = bad_slug ] || fail "disconnect bad slug"
rm -f "$sec/other.session_token"

# --- status ---
idir="$tmp/data/addons/actual/instances"
mkdir -p "$idir"
rm -f "$sec"/*
reset
printf 'right-SECRET' > "$STUB/server_pw"
inst() { # SLUG ACCESS [SERVER]
  printf '{"server":"%s","sync_id":"sync-a","profile":"default","access":"%s"}' "${3:-https://budget.example.com}" "$2" > "$idir/$1.json"
}
status_run() { # [ARGS...]
  brc=0
  STACK_PYTHON=host BUDGET_DATA_ROOT="$tmp/data" "$budget" status "$@" --json < /dev/null > "$tmp/jout" 2> "$tmp/jerr" || brc=$?
  cat "$tmp/jout" "$tmp/jerr" >> "$all"
}
inst main write
inst second read
printf 'tok-1' > "$sec/main.session_token"
printf 'tok-old' > "$sec/second.session_token"
status_run
[ "$brc" = 0 ] || fail "status exits 0 ($(cat "$tmp/jout" "$tmp/jerr"))"
jlines_ok || fail "every status line is JSON"
[ "$(wc -l < "$tmp/jout" | tr -d ' ')" = 3 ] || fail "two budget lines and done ($(cat "$tmp/jout"))"
grep -qx '{"phase":"budget","slug":"main","profile":"default","access":"write","state":"ok"}' "$tmp/jout" || fail "main is ok ($(cat "$tmp/jout"))"
grep -qx '{"phase":"budget","slug":"second","profile":"default","access":"read","state":"token_expired"}' "$tmp/jout" || fail "second is token_expired ($(cat "$tmp/jout"))"
[ "$(jget 3 phase)" = "done" ] || fail "status ends with done"
grep -q -- '-e ACTUAL_SESSION_TOKEN' "$STUB/docker.argv" || fail "status passes the token by name"

status_run --slug main
[ "$brc" = 0 ] && [ "$(wc -l < "$tmp/jout" | tr -d ' ')" = 2 ] && [ "$(jget 1 state)" = ok ] || fail "status --slug main ($(cat "$tmp/jout"))"
status_run --slug nope
[ "$brc" = 2 ] && [ "$(jget 1 error)" = not_found ] || fail "status --slug nope is not_found ($(cat "$tmp/jout"))"
status_run --slug Bad_Slug
[ "$brc" = 2 ] && [ "$(jget 1 error)" = bad_slug ] || fail "status bad slug"

# One state per exit code of check.
state_for() { # RC
  printf '%s' "$1" > "$STUB/admin_rc"
  status_run --slug main
  rm -f "$STUB/admin_rc"
  [ "$brc" = 0 ] || fail "status exits 0 on a bad state ($1)"
  jget 1 state
}
[ "$(state_for 2)" = encryption_password ] || fail "exit 2 is encryption_password"
[ "$(state_for 3)" = token_expired ] || fail "exit 3 is token_expired"
[ "$(state_for 4)" = timeout ] || fail "exit 4 is timeout"
[ "$(state_for 5)" = no_access ] || fail "exit 5 is no_access"
[ "$(state_for 6)" = server_unreachable ] || fail "exit 6 is server_unreachable"
[ "$(state_for 1)" = server_unreachable ] || fail "exit 1 is server_unreachable"

# bad stored access, no token file, an encryption password file, local.
inst main admin
status_run --slug main
[ "$(jget 1 state)" = bad_access ] || fail "stored access admin is bad_access ($(cat "$tmp/jout"))"
inst main read
rm -f "$sec/main.session_token"
status_run --slug main
[ "$(jget 1 state)" = token_expired ] || fail "no token file is token_expired"
printf 'tok-1' > "$sec/main.session_token"
printf 'enc-SECRET' > "$sec/main.encryption_password"
printf 'enc-SECRET' > "$STUB/enc_pw"
status_run --slug main
[ "$(jget 1 state)" = ok ] || fail "status passes the stored encryption password ($(cat "$tmp/jout"))"
rm -f "$STUB/enc_pw" "$sec/main.encryption_password"
inst main read local
status_run --slug main
tail -1 "$STUB/admin.log" | grep -q 'ACTUAL_SERVER_URL=http://actual-server:5006 ' || fail "a local row checks at the container URL"
rm -f "$idir"/*.json "$sec"/*
status_run
[ "$brc" = 0 ] && [ "$(wc -l < "$tmp/jout" | tr -d ' ')" = 1 ] && [ "$(jget 1 phase)" = "done" ] || fail "no rows is just done ($(cat "$tmp/jout"))"
grep -q 'status' <("$budget" --help) || fail "usage mentions status"

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

# access SLUG read|write: addon.sh configure and the Hermes reload, stubbed.
cat > "$tmp/bin/addon-stub" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB/addon.argv"
cat >> "$STUB/addon.stdin"
printf '\n' >> "$STUB/addon.stdin"
[[ ! -e "$STUB/addon_fail" ]] || { echo "configure refused" >&2; exit 3; }
SH
cat > "$tmp/bin/vault-stub" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB/vault.argv"
SH
chmod +x "$tmp/bin/addon-stub" "$tmp/bin/vault-stub"
access_run() { # ARGS...
  brc=0
  STACK_PYTHON=host BUDGET_DATA_ROOT="$tmp/data" BUDGET_ADDON_SH="$tmp/bin/addon-stub" BUDGET_VAULT_SH="$tmp/bin/vault-stub" \
    "$budget" access "$@" < /dev/null > "$tmp/jout" 2> "$tmp/jerr" || brc=$?
  cat "$tmp/jout" "$tmp/jerr" >> "$all"
}
rm -f "$STUB"/addon.argv "$STUB"/addon.stdin "$STUB"/vault.argv "$STUB"/addon_fail "$STUB/docker.argv"
inst main read
access_run main write --json
[ "$brc" = 0 ] || fail "access main write ($(cat "$tmp/jout" "$tmp/jerr"))"
grep -qx 'configure actual --instance main' "$STUB/addon.argv" || fail "access calls addon.sh configure ($(cat "$STUB/addon.argv"))"
grep -qx '{"access":"write"}' "$STUB/addon.stdin" || fail "access sends the value on stdin"
grep -q 'compose run --rm -e HERMES_BOOTSTRAP_FORCE=1 hermes-api-bootstrap' "$STUB/docker.argv" || fail "access runs the api bootstrap"
grep -q 'compose run --rm --no-deps -e HERMES_BOOTSTRAP_FORCE=1 hermes-browser-bootstrap' "$STUB/docker.argv" || fail "access runs the browser bootstrap"
grep -qx 'reload-plugins' "$STUB/vault.argv" || fail "access reloads the plugins"
grep -qx '{"phase":"done","mcp":\["budget-main-ro","budget-main"\]}' "$tmp/jout" || fail "write lists both names ($(cat "$tmp/jout"))"
access_run main read --json
grep -qx '{"phase":"done","mcp":\["budget-main-ro"\]}' "$tmp/jout" || fail "read lists the -ro name ($(cat "$tmp/jout"))"
: > "$STUB/addon.argv"
access_run main sudo --json
[ "$brc" = 2 ] && [ "$(jget 1 error)" = bad_access ] || fail "access main sudo is bad_access"
access_run nope write --json
[ "$brc" = 2 ] && [ "$(jget 1 error)" = not_found ] || fail "access nope write is not_found"
access_run Bad_Slug write --json
[ "$brc" = 2 ] && [ "$(jget 1 error)" = bad_slug ] || fail "access bad slug"
[ ! -s "$STUB/addon.argv" ] || fail "refused access calls addon.sh not at all"
touch "$STUB/addon_fail"
access_run main write --json
[ "$brc" = 4 ] && [ "$(jget 1 phase)" = error ] || fail "a configure failure is an error line ($(cat "$tmp/jout"))"
rm -f "$STUB/addon_fail" "$idir"/*.json
reset

# --- open ---
reset
run open && fail "open with no budget file must fail"
printf 'ACTUAL_SERVER_URL=http://actual-server:5006\nACTUAL_WEB_URL=http://localhost:5006/\nACTUAL_PASSWORD=open-SECRET\nACTUAL_BUDGET_SYNC_ID=s\n' > "$f"
chmod 600 "$f"
run open || fail "open ($(cat "$out"))"
grep -qx 'http://localhost:5006/' "$STUB/opened" || fail "open opens ACTUAL_WEB_URL"
[ "$(cat "$STUB/clip")" = open-SECRET ] || fail "open copies the server password"
grep -q 'clipboard' "$out" || fail "open says the password is on the clipboard"

# --- init --server-only ---
so_data="$tmp/so-data"
so_secrets="$so_data/addons/actual/secrets"
so_run() { # ARGS...
  brc=0
  STACK_PYTHON=host BUDGET_DATA_ROOT="$so_data" "$budget" init --server-only "$@" < /dev/null > "$tmp/jout" 2> "$tmp/jerr" || brc=$?
  cat "$tmp/jout" "$tmp/jerr" >> "$all"
}
so_reset() {
  reset
  rm -rf "$so_data" "$tmp/actual/server-password.pending"
  mkdir -p "$so_secrets"
}
# A fresh server: bootstrapped with a generated password kept in secrets/, a
# budget created, and none of the single-budget settings written.
so_reset
so_run --json
[ "$brc" = 0 ] || fail "init --server-only on a fresh server ($(cat "$tmp/jout" "$tmp/jerr"))"
jlines_ok || fail "init --server-only --json prints JSON lines only ($(cat "$tmp/jout"))"
[ -s "$STUB/server_pw" ] || fail "--server-only bootstraps the server"
[ "$(cat "$so_secrets/server_password")" = "$(cat "$STUB/server_pw")" ] || fail "--server-only keeps the generated password in secrets/server_password"
[ "$(mode "$so_secrets/server_password")" = 600 ] || fail "server_password is mode 600"
[ "$(wc -l < "$tmp/jout" | tr -d ' ')" = 1 ] || fail "--server-only prints one line ($(cat "$tmp/jout"))"
[ "$(jget 1 phase)" = "done" ] && [ "$(jget 1 sync_id)" = sync-new ] && [ "$(jget 1 name)" = Budget ] || fail "--server-only done carries sync_id and name ($(cat "$tmp/jout"))"
grep -q '^run create' "$STUB/admin.log" || fail "--server-only creates a Budget"
[ ! -e "$f" ] || fail "--server-only writes no budget.env"
grep -q 'ACTUAL_MCP_ENABLED=1' "$ENV_FILE" && fail "--server-only doesn't enable the MCP servers"
grep -q 'ACTUAL_LOCAL_ENABLED=1' "$ENV_FILE" && fail "--server-only leaves ACTUAL_LOCAL_ENABLED alone"
grep -qx 'ACTUAL_BUDGET_FILE=' "$ENV_FILE" || fail "--server-only leaves ACTUAL_BUDGET_FILE alone"
[ ! -e "$tmp/actual/server-password.pending" ] || fail "--server-only leaves no pending file"
pws="$pws $(cat "$STUB/server_pw")"

# Again: nothing created, same password; done names the budget the server lists.
budget_entry sync-new Budget > "$STUB/files.json"
: > "$STUB/admin.log"
cp "$so_secrets/server_password" "$tmp/pw.before"
so_run --json
[ "$brc" = 0 ] && [ "$(jget 1 sync_id)" = sync-new ] || fail "--server-only again ($(cat "$tmp/jout" "$tmp/jerr"))"
grep -q '^run create' "$STUB/admin.log" && fail "--server-only again creates nothing"
cmp -s "$so_secrets/server_password" "$tmp/pw.before" || fail "--server-only again keeps the password"

# Two budgets: no sync_id in done.
so_reset
printf 'srv-SECRET' > "$STUB/server_pw"
printf 'srv-SECRET' > "$so_secrets/server_password"
{ budget_entry sync-a Household; printf ','; budget_entry sync-b Business; } > "$STUB/files.json"
so_run --json
[ "$brc" = 0 ] && [ "$(wc -l < "$tmp/jout" | tr -d ' ')" = 1 ] && [ "$(jget 1 phase)" = "done" ] || fail "--server-only with two budgets ($(cat "$tmp/jout" "$tmp/jerr"))"
grep -q 'sync_id' "$tmp/jout" && fail "done has no sync_id when the server lists two budgets"
grep -q '^run create' "$STUB/admin.log" 2>/dev/null && fail "--server-only creates nothing beside two budgets"

# A server set up by init: its budget.env password is adopted, no new password.
so_reset
printf 'srv-SECRET' > "$STUB/server_pw"
budget_entry sync-local "My Budget" > "$STUB/files.json"
printf 'ACTUAL_PASSWORD=srv-SECRET\nACTUAL_BUDGET_SYNC_ID=sync-local\n' > "$f"
so_run --json
[ "$brc" = 0 ] && [ "$(jget 1 sync_id)" = sync-local ] && [ "$(jget 1 name)" = "My Budget" ] || fail "--server-only adopts a server set up by init ($(cat "$tmp/jout" "$tmp/jerr"))"
[ "$(cat "$STUB/server_pw")" = srv-SECRET ] || fail "--server-only sets no new password on a bootstrapped server"
cmp -s <(printf 'ACTUAL_PASSWORD=srv-SECRET\nACTUAL_BUDGET_SYNC_ID=sync-local\n') "$f" || fail "--server-only leaves budget.env alone"

# A stale stored password and the right one in budget.env: retried, then refreshed.
so_reset
printf 'new-SECRET' > "$STUB/server_pw"
printf 'old-SECRET' > "$so_secrets/server_password"
budget_entry sync-local "My Budget" > "$STUB/files.json"
printf 'ACTUAL_PASSWORD=new-SECRET\nACTUAL_BUDGET_SYNC_ID=sync-local\n' > "$f"
so_run --json
[ "$brc" = 0 ] && [ "$(jget 1 sync_id)" = sync-local ] || fail "--server-only retries with budget.env's password ($(cat "$tmp/jout" "$tmp/jerr"))"
[ "$(cat "$so_secrets/server_password")" = new-SECRET ] || fail "--server-only refreshes the stale server_password"
[ "$(mode "$so_secrets/server_password")" = 600 ] || fail "the refreshed server_password is mode 600"

# Docker down: an error line and exit 4 in --json mode.
so_reset
# shellcheck disable=SC2016
printf '#!/bin/sh\nif [ "$1" = info ]; then exit 1; fi\nexec "%s/bin/docker.real" "$@"\n' "$tmp" > "$tmp/bin/docker.new"
mv "$tmp/bin/docker" "$tmp/bin/docker.real"
mv "$tmp/bin/docker.new" "$tmp/bin/docker"
chmod +x "$tmp/bin/docker"
so_run --json
[ "$brc" = 4 ] && [ "$(jget 1 error)" = server_unreachable ] || fail "--server-only with Docker down ($(cat "$tmp/jout" "$tmp/jerr"))"
mv "$tmp/bin/docker.real" "$tmp/bin/docker"

# Bootstrapped, password unknown: login_refused.
so_reset
printf 'srv-SECRET' > "$STUB/server_pw"
budget_entry sync-local "My Budget" > "$STUB/files.json"
so_run --json
[ "$brc" = 4 ] && [ "$(jget 1 error)" = login_refused ] || fail "--server-only with no known password is login_refused ($(cat "$tmp/jout" "$tmp/jerr"))"
[ ! -e "$so_secrets/server_password" ] || fail "a refused --server-only stores no password"
printf 'wrong-SECRET' > "$so_secrets/server_password"
so_run --json
[ "$brc" = 4 ] && [ "$(jget 1 error)" = login_refused ] || fail "--server-only with a wrong stored password is login_refused"

# Server down, create timeout.
so_reset
touch "$STUB/down"
so_run --json
[ "$brc" = 4 ] && [ "$(jget 1 error)" = server_unreachable ] || fail "--server-only with no server is server_unreachable ($(cat "$tmp/jout"))"
rm -f "$STUB/down"
touch "$STUB/timeout"
so_run --json
[ "$brc" = 4 ] && [ "$(jget 1 error)" = timeout ] || fail "--server-only create timeout ($(cat "$tmp/jout" "$tmp/jerr"))"
rm -f "$STUB/timeout"
pws="$pws $(cat "$STUB/server_pw" 2>/dev/null || true)"

# Without --json: log lines, no JSON.
so_reset
so_run
[ "$brc" = 0 ] || fail "--server-only without --json ($(cat "$tmp/jout" "$tmp/jerr"))"
grep -q '^\[budget\]' "$tmp/jout" || fail "--server-only without --json logs ($(cat "$tmp/jout" "$tmp/jerr"))"
pws="$pws $(cat "$STUB/server_pw")"

# --- remove ---
printf 'x' > "$f"
printf 'ACTUAL_MCP_ENABLED=1\nACTUAL_LOCAL_ENABLED=1\nACTUAL_BUDGET_FILE=%s\n' "$f" > "$ENV_FILE"
BUDGET_DATA_ROOT="$tmp/data" run remove || fail "remove ($(cat "$out"))"
[ ! -e "$f" ] || fail "remove deletes budget.env"
grep -q 'docker compose stop actual-mcp-rw actual-mcp-ro' "$out" || fail "remove says to stop the MCP containers"
grep -q 'make hermes-reload' "$out" || fail "remove says to run make hermes-reload"
grep -qx 'ACTUAL_MCP_ENABLED=0' "$ENV_FILE" || fail "remove sets ACTUAL_MCP_ENABLED=0"
grep -qx 'ACTUAL_BUDGET_FILE=' "$ENV_FILE" || fail "remove clears ACTUAL_BUDGET_FILE"
grep -qx 'ACTUAL_LOCAL_ENABLED=1' "$ENV_FILE" || fail "remove leaves the local server on"
BUDGET_DATA_ROOT="$tmp/data" run remove && fail "remove with no budget file must fail"

# remove beside connected budgets keeps the MCP servers on.
mkdir -p "$tmp/data/addons/actual/instances"
printf '{}' > "$tmp/data/addons/actual/instances/main.json"
printf 'ACTUAL_MCP_ENABLED=1\nACTUAL_LOCAL_ENABLED=1\nACTUAL_BUDGET_FILE=%s\n' "$f" > "$ENV_FILE"
printf 'x' > "$f"
BUDGET_DATA_ROOT="$tmp/data" run remove || fail "remove beside a row ($(cat "$out"))"
[ ! -e "$f" ] || fail "remove beside a row deletes budget.env"
grep -qx 'ACTUAL_MCP_ENABLED=1' "$ENV_FILE" || fail "remove leaves ACTUAL_MCP_ENABLED=1 while a budget is connected"
grep -qx 'ACTUAL_BUDGET_FILE=' "$ENV_FILE" || fail "remove beside a row clears ACTUAL_BUDGET_FILE"
rm -f "$tmp/data/addons/actual/instances/main.json"
printf 'x' > "$f"
BUDGET_DATA_ROOT="$tmp/data" run remove || fail "remove with no rows ($(cat "$out"))"
grep -qx 'ACTUAL_MCP_ENABLED=0' "$ENV_FILE" || fail "remove with no rows sets ACTUAL_MCP_ENABLED=0"

run frobnicate && fail "unknown subcommand must fail"

# --- No password in any output, or in curl or docker argv ---
for s in right-SECRET wrong-SECRET enc-SECRET bad-enc-SECRET srv-SECRET open-SECRET check-SECRET url-SECRET "$quoted" "$unstorable" line-SECRET $pws; do
  for log in "$all" "$STUB/curl.argv" "$STUB/docker.argv"; do
    if grep -qF -- "$s" "$log"; then fail "${log##*/} contains a password"; fi
  done
done

printf 'OK budget.sh\n'
