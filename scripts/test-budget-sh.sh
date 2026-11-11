#!/usr/bin/env bash
# shellcheck disable=SC2015  # "A && B || fail" is the check: fail runs when either test is false
# scripts/budget.sh against stubs: a fake Actual server behind a stub curl, a
# stub docker that plays actual-admin.mjs, and stub uname/id/open/pbcopy. No
# real Actual server, container or budget is touched.
#
#   bash scripts/test-budget-sh.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
budget="${ROOT}/scripts/budget.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

export ENV_FILE="$tmp/.env"
export STUB="$tmp/stub"
mkdir -p "$tmp/bin" "$STUB"

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
printf 'run %s ACTUAL_SERVER_URL=%s \n' "$sub" "${ACTUAL_SERVER_URL:-}" >> "$STUB/admin.log"
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
  (cd "$STUB" && rm -f server_pw files.json enc_pw list.tsv down html curl_timeout admin_rc admin.env admin.log opened clip)
  printf 'COMPOSE_PROFILES=core\nACTUAL_MCP_ENABLED=0\nACTUAL_LOCAL_ENABLED=0\n' > "$ENV_FILE"
}
pws=""
mode() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }
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
jget() { # LINE FIELD (FIELD may be a.b for a nested object)
  sed -n "${1}p" "$tmp/jout" | python3 -c 'import json,sys; v=json.load(sys.stdin)
for k in sys.argv[1].split("."): v=v[k]
print(json.dumps(v) if isinstance(v,bool) else v)' "$2"
}

# --- removed commands ---
# The single budget (budget.env) is gone: add, check, remove, init without
# --server-only and access with no slug all refuse.
reset
run add --url https://budget.example.com && fail "add is gone"
run check && fail "check is gone"
run remove && fail "remove is gone"
run init && fail "init needs --server-only"
grep -q -- '--server-only' "$out" || fail "bare init names --server-only ($(cat "$out"))"
run init --rotate && fail "init --rotate is gone"
run init --frob && fail "init rejects unknown options"
run access read && fail "access with no slug is gone"
run access write && fail "access with no slug is gone"
if grep -q '^ACTUAL_ACCESS=' "$ENV_FILE"; then fail "access with no slug writes nothing to .env"; fi
if "$budget" --help | grep -q 'budget\.env'; then fail "usage names no budget.env"; fi

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
jget 1 message | grep -q 'addon.sh enable actual --reinit' || fail "not_found names addon.sh enable actual --reinit ($(cat "$tmp/jout"))"
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
  # CONNECT_FLAGS: extra flags, split on spaces.
  # shellcheck disable=SC2086
  printf '%s' "$2" | STACK_PYTHON=host BUDGET_DATA_ROOT="$tmp/data" "$budget" connect --slug "$1" ${CONNECT_FLAGS:-} --json > "$tmp/jout" 2> "$tmp/jerr" || brc=$?
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
grep -qx '{"phase":"done","account":"main","mcp":\["budget-main"\]}' "$tmp/jout" || fail "connect lists budget-main ($(cat "$tmp/jout"))"
grep -q 'token=tok-1 sync=sync-a' "$STUB/admin.env" || fail "check gets the token and sync id"
grep -q 'ACTUAL_SERVER_URL=https://budget.example.com ' "$STUB/admin.log" || fail "check uses the remote URL"
grep -q -- '-e ACTUAL_SESSION_TOKEN' "$STUB/docker.argv" || fail "docker gets the token by name"
! grep -rqF right-SECRET "$tmp/data" || fail "no file holds the password"
rm -f "$sec"/main.*

# A bind is addon.sh's business: the hook ignores it.
connect_run main "{$srv,\"password\":\"right-SECRET\",\"bind\":{\"default\":\"rw\",\"browser\":\"rw\",\"api-server\":\"ro\"},\"encryption_password\":\"enc-SECRET\"}"
[ "$brc" = 0 ] || fail "connect with a bind ($(cat "$tmp/jout" "$tmp/jerr"))"
grep -qx '{"phase":"done","account":"main","mcp":\["budget-main"\]}' "$tmp/jout" || fail "connect with a bind lists budget-main once ($(cat "$tmp/jout"))"
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
# addon_store.SLUG_RE's rule: a slug starts with a letter.
connect_run 9lives "$pwin"
[ "$brc" = 2 ] && [ "$(jget 1 error)" = bad_slug ] || fail "a slug starting with a digit is bad_slug"
python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import addon_store; sys.exit(0 if not addon_store.SLUG_RE.match("9lives") and addon_store.SLUG_RE.match("a9-x") else 1)' "${ROOT}/scripts/lib" \
  || fail "addon_store agrees on 9lives and a9-x"
connect_run a9-x "$pwin"
[ "$brc" = 0 ] || fail "a9-x is a valid slug ($(cat "$tmp/jout" "$tmp/jerr"))"
rm -f "$sec"/a9-x.*
connect_run 'a
b' "$pwin"
[ "$brc" = 2 ] && [ "$(jget 1 error)" = bad_slug ] || fail "a slug with a newline is bad_slug"
nofiles || fail "input errors leave secrets empty"
# Linux as a uid other than 1000: the containers couldn't read the files.
: > "$STUB/curl.argv"
STUB_UNAME=Linux STUB_UID=1001 connect_run main "$pwin"
[ "$brc" = 2 ] && [ "$(jget 1 error)" = not_found ] || fail "connect on Linux as uid 1001 ($(cat "$tmp/jout" "$tmp/jerr"))"
jget 1 message | grep -q 'uid 1000' || fail "the uid refusal names uid 1000 ($(cat "$tmp/jout"))"
[ "$(wc -l < "$tmp/jout" | tr -d ' ')" = 1 ] || fail "the uid refusal is one JSON line"
[ ! -s "$STUB/curl.argv" ] || fail "the uid refusal signs in nowhere"
nofiles || fail "the uid refusal writes nothing"
STUB_UNAME=Linux STUB_UID=1000 connect_run main "$pwin"
[ "$brc" = 0 ] || fail "connect on Linux as uid 1000 ($(cat "$tmp/jout" "$tmp/jerr"))"
rm -f "$sec"/main.*
STUB_UNAME=Darwin STUB_UID=501 connect_run main "$pwin"
[ "$brc" = 0 ] || fail "connect on macOS as uid 501 ($(cat "$tmp/jout" "$tmp/jerr"))"
rm -f "$sec"/main.*

# --replace (addon.sh connect --force): the same checks in the same order,
# nothing written until they pass, then the old secrets are overwritten.
old_secrets() {
  printf 'tok-OLD' > "$sec/main.session_token"
  printf 'enc-OLD' > "$sec/main.encryption_password"
  chmod 600 "$sec/main.session_token" "$sec/main.encryption_password"
}
old_secrets
CONNECT_FLAGS=--replace connect_run main "$pwin"
[ "$brc" = 0 ] || fail "connect --replace ($(cat "$tmp/jout" "$tmp/jerr"))"
grep -qx '{"phase":"done","account":"main","mcp":\["budget-main"\]}' "$tmp/jout" || fail "connect --replace done line ($(cat "$tmp/jout"))"
[ "$(cat "$sec/main.session_token")" = tok-1 ] || fail "connect --replace overwrites the session token"
[ "$(mode "$sec/main.session_token")" = 600 ] || fail "the replaced token file is mode 600"
[ ! -e "$sec/main.encryption_password" ] || fail "connect --replace without an encryption password removes the old one"
compgen -G "$sec/.write.*" > /dev/null && fail "connect --replace leaves no temp file"
old_secrets
CONNECT_FLAGS=--replace connect_run main "{$srv,\"password\":\"right-SECRET\",\"encryption_password\":\"enc-NEW\"}"
[ "$brc" = 0 ] || fail "connect --replace with an encryption password ($(cat "$tmp/jout" "$tmp/jerr"))"
[ "$(cat "$sec/main.encryption_password")" = enc-NEW ] || fail "connect --replace overwrites the encryption password"
[ "$(mode "$sec/main.encryption_password")" = 600 ] || fail "the replaced encryption file is mode 600"
rm -f "$sec"/main.*

# A failed check under --replace leaves the old secrets as they were.
replace_fails() { # LABEL RC NAME
  [ "$brc" = "$2" ] && [ "$(jget 1 error)" = "$3" ] || fail "--replace $1: want $3/$2 ($(cat "$tmp/jout" "$tmp/jerr"))"
  [ "$(cat "$sec/main.session_token")" = tok-OLD ] || fail "--replace $1 keeps the old token"
  [ "$(cat "$sec/main.encryption_password")" = enc-OLD ] || fail "--replace $1 keeps the old encryption password"
}
old_secrets
CONNECT_FLAGS=--replace connect_run main "{$srv,\"password\":\"wrong-SECRET\"}"; replace_fails "refused password" 4 login_refused
printf '4' > "$STUB/admin_rc"; CONNECT_FLAGS=--replace connect_run main "$pwin"; replace_fails "admin 4" 4 timeout
printf '5' > "$STUB/admin_rc"; CONNECT_FLAGS=--replace connect_run main "$pwin"; replace_fails "admin 5" 4 no_access
rm -f "$STUB/admin_rc"
printf 'enc-SECRET' > "$STUB/enc_pw"
CONNECT_FLAGS=--replace connect_run main "$pwin"; replace_fails "missing encryption password" 4 encryption_password
rm -f "$STUB/enc_pw"
CONNECT_FLAGS=--replace connect_run Bad_Slug "$pwin"
[ "$brc" = 2 ] && [ "$(jget 1 error)" = bad_slug ] || fail "--replace with a bad slug"
rm -f "$sec"/main.*
CONNECT_FLAGS="--replace --replace" connect_run main "$pwin"
[ "$brc" = 0 ] || fail "--replace twice is accepted"
rm -f "$sec"/main.*

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
# No files (a half-finished connect, hand-deleted secrets): still done, so
# addon.sh goes on to remove the row.
disconnect_run nope
[ "$brc" = 0 ] && [ "$(cat "$tmp/jout")" = '{"phase":"done"}' ] || fail "disconnect with no files is done ($(cat "$tmp/jout" "$tmp/jerr"))"
[ -e "$sec/other.session_token" ] || fail "disconnect with no files touches nothing else"
disconnect_run Bad_Slug
[ "$brc" = 2 ] && [ "$(jget 1 error)" = bad_slug ] || fail "disconnect bad slug"
rm -f "$sec/other.session_token"

# --- status ---
idir="$tmp/data/addons/actual/instances"
mkdir -p "$idir"
rm -f "$sec"/*
reset
printf 'right-SECRET' > "$STUB/server_pw"
inst() { # SLUG ACCESS [SERVER]: ACCESS write binds default and browser rw,
  # read binds every profile ro, none writes no bind.
  local bind=""
  case "$2" in
    write) bind=',"bind":{"default":"rw","browser":"rw","api-server":"ro"}' ;;
    read) bind=',"bind":{"default":"ro","browser":"ro","api-server":"ro"}' ;;
  esac
  printf '{"server":"%s","sync_id":"sync-a"%s}' "${3:-https://budget.example.com}" "$bind" > "$idir/$1.json"
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
grep -qx '{"phase":"budget","slug":"main","access":"write","state":"ok"}' "$tmp/jout" || fail "main is ok ($(cat "$tmp/jout"))"
grep -qx '{"phase":"budget","slug":"second","access":"read","state":"token_expired"}' "$tmp/jout" || fail "second is token_expired ($(cat "$tmp/jout"))"
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

# access from bind: no bind, or one that isn't a map, reads as read; only
# default decides.
inst main none
status_run --slug main
grep -qx '{"phase":"budget","slug":"main","access":"read","state":"ok"}' "$tmp/jout" || fail "no bind is read ($(cat "$tmp/jout"))"
printf '{"server":"https://budget.example.com","sync_id":"sync-a","bind":"rw"}' > "$idir/main.json"
status_run --slug main
[ "$(jget 1 access)" = read ] && [ "$(jget 1 state)" = ok ] || fail "a bind that isn't a map is read ($(cat "$tmp/jout"))"
printf '{"server":"https://budget.example.com","sync_id":"sync-a","bind":{"browser":"rw"}}' > "$idir/main.json"
status_run --slug main
[ "$(jget 1 access)" = read ] || fail "access follows bind.default only ($(cat "$tmp/jout"))"
printf '{"server":"https://budget.example.com","sync_id":"sync-a","profile":"default","access":"write"}' > "$idir/main.json"
status_run --slug main
grep -qx '{"phase":"budget","slug":"main","access":"read","state":"ok"}' "$tmp/jout" || fail "old access/profile keys are ignored ($(cat "$tmp/jout"))"
# no token file, an encryption password file, local.
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
printf 'COMPOSE_PROFILES=core\nACTUAL_LOCAL_ENABLED=1\n' > "$ENV_FILE"
inst main read local
status_run --slug main
tail -1 "$STUB/admin.log" | grep -q 'ACTUAL_SERVER_URL=http://actual-server:5006 ' || fail "a local row checks at the container URL"
# The local server off: a local row is server_off without a sign-in or a
# container (even with no token); a remote row in the same run is checked.
for lv in 0 "" no; do
  rm -f "$idir"/*.json "$sec"/*
  printf 'COMPOSE_PROFILES=core\nACTUAL_LOCAL_ENABLED=%s\n' "$lv" > "$ENV_FILE"
  inst loc write local
  inst main read
  printf 'tok-1' > "$sec/main.session_token"
  : > "$STUB/admin.log"
  : > "$STUB/curl.argv"
  status_run
  [ "$brc" = 0 ] || fail "status with the local server off ('${lv}') exits 0 ($(cat "$tmp/jout" "$tmp/jerr"))"
  jlines_ok || fail "every status line is JSON with the local server off"
  grep -qx '{"phase":"budget","slug":"loc","access":"write","state":"server_off"}' "$tmp/jout" || fail "a local row is server_off ('${lv}') ($(cat "$tmp/jout"))"
  grep -qx '{"phase":"budget","slug":"main","access":"read","state":"ok"}' "$tmp/jout" || fail "a remote row is still checked ($(cat "$tmp/jout"))"
  [ "$(wc -l < "$tmp/jout" | tr -d ' ')" = 3 ] || fail "two budget lines and done ($(cat "$tmp/jout"))"
  [ "$(grep -c . "$STUB/admin.log")" = 1 ] || fail "only the remote row is checked by a container ($(cat "$STUB/admin.log"))"
  ! grep -q 'actual-server' "$STUB/admin.log" || fail "only the remote row is checked by a container ($(cat "$STUB/admin.log"))"
  [ ! -s "$STUB/curl.argv" ] || fail "status signs in nowhere ($(cat "$STUB/curl.argv"))"
done
status_run --slug loc
[ "$brc" = 0 ] && [ "$(jget 1 state)" = server_off ] || fail "status --slug on a local row with the server off ($(cat "$tmp/jout"))"
rm -f "$sec/main.session_token"
status_run --slug loc
[ "$(jget 1 state)" = server_off ] || fail "server_off needs no token file ($(cat "$tmp/jout"))"
printf 'COMPOSE_PROFILES=core\nACTUAL_LOCAL_ENABLED=1\n' > "$ENV_FILE"
status_run --slug loc
[ "$(jget 1 state)" = token_expired ] || fail "with the local server on the local row is checked again ($(cat "$tmp/jout"))"

rm -f "$idir"/*.json "$sec"/*
status_run
[ "$brc" = 0 ] && [ "$(wc -l < "$tmp/jout" | tr -d ' ')" = 1 ] && [ "$(jget 1 phase)" = "done" ] || fail "no rows is just done ($(cat "$tmp/jout"))"
grep -q 'status' <("$budget" --help) || fail "usage mentions status"

# --- access ---
reset

# access SLUG read|write: addon.sh configure and the Hermes reload, stubbed.
# addon.sh: logs argv and stdin; prints $STUB/addon.out on stdout and exits
# with $STUB/addon.rc when they exist.
cat > "$tmp/bin/addon-stub" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB/addon.argv"
cat >> "$STUB/addon.stdin"
printf '\n' >> "$STUB/addon.stdin"
[[ ! -e "$STUB/addon_fail" ]] || { echo "configure refused" >&2; exit 3; }
[[ ! -e "$STUB/addon.out" ]] || cat "$STUB/addon.out"
[[ ! -e "$STUB/addon.err" ]] || cat "$STUB/addon.err" >&2
exit "$(cat "$STUB/addon.rc" 2>/dev/null || echo 0)"
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
grep -qx '{"bind":{"default":"rw","browser":"rw","api-server":"ro"}}' "$STUB/addon.stdin" || fail "access write sends the bind on stdin ($(cat "$STUB/addon.stdin"))"
grep -q 'compose run --rm -e HERMES_BOOTSTRAP_FORCE=1 hermes-api-bootstrap' "$STUB/docker.argv" || fail "access runs the api bootstrap"
grep -q 'compose run --rm --no-deps -e HERMES_BOOTSTRAP_FORCE=1 hermes-browser-bootstrap' "$STUB/docker.argv" || fail "access runs the browser bootstrap"
grep -qx 'reload-plugins' "$STUB/vault.argv" || fail "access reloads the plugins"
grep -qx '{"phase":"done","mcp":\["budget-main"\]}' "$tmp/jout" || fail "write lists budget-main ($(cat "$tmp/jout"))"
: > "$STUB/addon.stdin"
access_run main read --json
[ "$brc" = 0 ] || fail "access main read ($(cat "$tmp/jout" "$tmp/jerr"))"
grep -qx '{"bind":{"default":"ro","browser":"ro","api-server":"ro"}}' "$STUB/addon.stdin" || fail "access read sends the ro bind ($(cat "$STUB/addon.stdin"))"
grep -qx '{"phase":"done","mcp":\["budget-main"\]}' "$tmp/jout" || fail "read lists budget-main ($(cat "$tmp/jout"))"
access_run main read
[ "$brc" = 0 ] && grep -q '^\[budget\] Set access read for main' "$tmp/jout" || fail "access without --json logs a line ($(cat "$tmp/jout" "$tmp/jerr"))"
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
run open && fail "open with the local server off must fail"
grep -q 'ACTUAL_LOCAL_ENABLED' "$out" || fail "open says the local server is off ($(cat "$out"))"
printf 'COMPOSE_PROFILES=core\nACTUAL_LOCAL_ENABLED=1\nACTUAL_PORT=5099\n' > "$ENV_FILE"
mkdir -p "$tmp/open-data/addons/actual/secrets"
BUDGET_DATA_ROOT="$tmp/open-data" run open || fail "open without a stored password ($(cat "$out"))"
grep -qx 'http://localhost:5099/' "$STUB/opened" || fail "open opens the local web app on ACTUAL_PORT"
[ ! -e "$STUB/clip" ] || fail "open copies nothing without a stored password"
grep -q -- '--reinit' "$out" || fail "open says how to get a stored password ($(cat "$out"))"
printf 'open-SECRET' > "$tmp/open-data/addons/actual/secrets/server_password"
BUDGET_DATA_ROOT="$tmp/open-data" run open || fail "open ($(cat "$out"))"
[ "$(cat "$STUB/clip")" = open-SECRET ] || fail "open copies the stored server password"
grep -q 'clipboard' "$out" || fail "open says the password is on the clipboard"
run open --x && fail "open takes no options"
reset

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
  printf 'COMPOSE_PROFILES=core\nACTUAL_MCP_ENABLED=0\nACTUAL_LOCAL_ENABLED=1\n' > "$ENV_FILE"
  rm -rf "$so_data"
  mkdir -p "$so_secrets"
}
# The local server off (a remote-only install): done, with nothing probed or
# written, so addon.sh enable actual goes through.
for lv in 0 "" no; do
  so_reset
  if [[ -n "$lv" ]]; then
    printf 'COMPOSE_PROFILES=core\nACTUAL_LOCAL_ENABLED=%s\n' "$lv" > "$ENV_FILE"
  else
    printf 'COMPOSE_PROFILES=core\n' > "$ENV_FILE"
  fi
  cp "$ENV_FILE" "$tmp/env.before"
  rm -f "$STUB/curl.argv.so" "$STUB/docker.argv.so"
  cp "$STUB/curl.argv" "$STUB/curl.argv.so" 2>/dev/null || : > "$STUB/curl.argv.so"
  cp "$STUB/docker.argv" "$STUB/docker.argv.so" 2>/dev/null || : > "$STUB/docker.argv.so"
  so_run --json
  [ "$brc" = 0 ] && [ "$(cat "$tmp/jout")" = '{"phase":"done"}' ] || fail "--server-only with ACTUAL_LOCAL_ENABLED='$lv' is done ($(cat "$tmp/jout" "$tmp/jerr"))"
  cmp -s "$STUB/curl.argv" "$STUB/curl.argv.so" || fail "--server-only with the local server off probes nothing"
  cmp -s "$STUB/docker.argv" "$STUB/docker.argv.so" || fail "--server-only with the local server off runs no docker"
  [ -z "$(ls -A "$so_secrets")" ] || fail "--server-only with the local server off writes no secret"
  cmp -s "$ENV_FILE" "$tmp/env.before" || fail "--server-only with the local server off leaves .env alone"
done
# Off without --json: a log line, exit 0.
so_reset
printf 'COMPOSE_PROFILES=core\nACTUAL_LOCAL_ENABLED=0\n' > "$ENV_FILE"
so_run
[ "$brc" = 0 ] || fail "--server-only off without --json ($(cat "$tmp/jout" "$tmp/jerr"))"
grep -q 'ACTUAL_LOCAL_ENABLED' "$tmp/jout" || fail "--server-only off without --json logs why ($(cat "$tmp/jout" "$tmp/jerr"))"
grep -q -- '--reinit' "$tmp/jout" || fail "--server-only off names addon.sh enable actual --reinit"
# A fresh server: bootstrapped with a generated password kept in secrets/, a
# budget created, and ACTUAL_MCP_ENABLED left alone.
so_reset
so_run --json
[ "$brc" = 0 ] || fail "init --server-only on a fresh server ($(cat "$tmp/jout" "$tmp/jerr"))"
jlines_ok || fail "init --server-only --json prints JSON lines only ($(cat "$tmp/jout"))"
[ -s "$STUB/server_pw" ] || fail "--server-only bootstraps the server"
[ "$(cat "$so_secrets/server_password")" = "$(cat "$STUB/server_pw")" ] || fail "--server-only keeps the generated password in secrets/server_password"
[ "$(mode "$so_secrets/server_password")" = 600 ] || fail "server_password is mode 600"
[ "$(wc -l < "$tmp/jout" | tr -d ' ')" = 1 ] || fail "--server-only prints one line ($(cat "$tmp/jout"))"
[ "$(jget 1 phase)" = "done" ] && [ "$(jget 1 sync_id)" = sync-new ] && [ "$(jget 1 name)" = Budget ] || fail "--server-only done carries sync_id and name ($(cat "$tmp/jout"))"
[ "$(jget 1 result.sync_id)" = sync-new ] && [ "$(jget 1 result.name)" = Budget ] || fail "--server-only done carries the budget as result ($(cat "$tmp/jout"))"
grep -q '^run create' "$STUB/admin.log" || fail "--server-only creates a Budget"
grep -q 'ACTUAL_MCP_ENABLED=1' "$ENV_FILE" && fail "--server-only doesn't enable the MCP servers"
grep -qx 'ACTUAL_LOCAL_ENABLED=1' "$ENV_FILE" || fail "--server-only leaves ACTUAL_LOCAL_ENABLED alone"
pws="$pws $(cat "$STUB/server_pw")"

# Again: nothing created, same password; done names the budget the server lists.
budget_entry sync-new Budget > "$STUB/files.json"
: > "$STUB/admin.log"
cp "$so_secrets/server_password" "$tmp/pw.before"
so_run --json
[ "$brc" = 0 ] && [ "$(jget 1 sync_id)" = sync-new ] && [ "$(jget 1 result.sync_id)" = sync-new ] && [ "$(jget 1 result.name)" = Budget ] || fail "--server-only again ($(cat "$tmp/jout" "$tmp/jerr"))"
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
grep -q 'result' "$tmp/jout" && fail "done has no result when the server lists two budgets"
grep -q '^run create' "$STUB/admin.log" 2>/dev/null && fail "--server-only creates nothing beside two budgets"

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

# --- connect-cli (make budget-connect) ---
# The passwords go to addon.sh on stdin; with them in the environment nothing
# is asked. addon.sh is the stub above.
cli_run() { # ARGS... (environment as set by the caller)
  brc=0
  rm -f "$STUB/addon.argv" "$STUB/addon.stdin"
  STACK_PYTHON=host BUDGET_DATA_ROOT="$tmp/data" BUDGET_ADDON_SH="$tmp/bin/addon-stub" \
    "$budget" connect-cli "$@" < /dev/null > "$tmp/jout" 2> "$tmp/jerr" || brc=$?
  cat "$tmp/jout" "$tmp/jerr" >> "$all"
}
reset
printf 'cli-SECRET' > "$STUB/server_pw"
budget_entry sync-only "Only" > "$STUB/files.json"
# The add-on off: refused before anything is asked or sent.
ACTUAL_PASSWORD=cli-SECRET cli_run --slug home --url https://budget.example.com --sync-id sync-x
[ "$brc" != 0 ] && [ ! -e "$STUB/addon.argv" ] || fail "connect-cli with the add-on off must not call addon.sh"
grep -q 'the Actual add-on is off; run scripts/addon.sh enable actual first' "$tmp/jerr" || fail "connect-cli says the add-on is off ($(cat "$tmp/jerr"))"
printf 'COMPOSE_PROFILES=core\nACTUAL_MCP_ENABLED=1\nACTUAL_LOCAL_ENABLED=0\n' > "$ENV_FILE"
printf '{"phase":"setup","step":"register","status":"running"}\n{"phase":"done","id":"actual","action":"connect","restart":[],"mcp":["budget-home"],"account":"home"}\n' > "$STUB/addon.out"
ACTUAL_PASSWORD=cli-SECRET ACTUAL_BUDGET_ENCRYPTION_PASSWORD='cli-enc"SECRET' \
  cli_run --slug home --url https://budget.example.com --access write
[ "$brc" = 0 ] || fail "connect-cli ($(cat "$tmp/jout" "$tmp/jerr"))"
[ "$(cat "$tmp/jout")" = '[budget] Connected home; Hermes entries: budget-home' ] || fail "connect-cli prints a [budget] line, not JSON ($(cat "$tmp/jout"))"
rm -f "$STUB/addon.out"
grep -qx 'connect actual --instance home --json' "$STUB/addon.argv" || fail "connect-cli calls addon.sh connect ($(cat "$STUB/addon.argv"))"
python3 - "$STUB/addon.stdin" <<'PY' || fail "connect-cli sends the connect JSON on stdin"
import json, sys
d = json.loads(open(sys.argv[1]).read())
assert d == {"server": "https://budget.example.com", "sync_id": "sync-only",
             "bind": {"default": "rw", "browser": "rw", "api-server": "ro"},
             "password": "cli-SECRET", "encryption_password": 'cli-enc"SECRET'}, d
PY
ACTUAL_PASSWORD=cli-SECRET cli_run --slug home --url https://budget.example.com --sync-id sync-x
python3 - "$STUB/addon.stdin" <<'PY' || fail "connect-cli with a sync id and no encryption password"
import json, sys
d = json.loads(open(sys.argv[1]).read())
assert d == {"server": "https://budget.example.com", "sync_id": "sync-x", "password": "cli-SECRET"}, d
PY
# --force goes to addon.sh connect, nothing else changes.
ACTUAL_PASSWORD=cli-SECRET cli_run --slug home --url https://budget.example.com --sync-id sync-x --force
[ "$brc" = 0 ] || fail "connect-cli --force ($(cat "$tmp/jout" "$tmp/jerr"))"
grep -qx 'connect actual --instance home --force --json' "$STUB/addon.argv" || fail "connect-cli --force passes --force to addon.sh connect ($(cat "$STUB/addon.argv"))"
python3 - "$STUB/addon.stdin" <<'PY' || fail "connect-cli --force sends the same JSON"
import json, sys
d = json.loads(open(sys.argv[1]).read())
assert d == {"server": "https://budget.example.com", "sync_id": "sync-x", "password": "cli-SECRET"}, d
PY
# --access read sends no bind; --access admin is refused before addon.sh.
ACTUAL_PASSWORD=cli-SECRET cli_run --slug home --url https://budget.example.com --sync-id sync-x --access read
python3 - "$STUB/addon.stdin" <<'PY' || fail "connect-cli --access read sends no bind"
import json, sys
d = json.loads(open(sys.argv[1]).read())
assert d == {"server": "https://budget.example.com", "sync_id": "sync-x", "password": "cli-SECRET"}, d
PY
ACTUAL_PASSWORD=cli-SECRET cli_run --slug home --url https://budget.example.com --sync-id sync-x --access admin
[ "$brc" != 0 ] && [ ! -e "$STUB/addon.argv" ] || fail "connect-cli --access admin must not call addon.sh"
# local: no server password.
cli_run --slug mine --url local --sync-id sync-l
[ "$brc" = 0 ] || fail "connect-cli local ($(cat "$tmp/jerr"))"
python3 - "$STUB/addon.stdin" <<'PY' || fail "connect-cli local sends no password"
import json, sys
d = json.loads(open(sys.argv[1]).read())
assert d == {"server": "local", "sync_id": "sync-l"}, d
PY
# No password and no terminal: refused, addon.sh not called.
cli_run --slug home --url https://budget.example.com --sync-id sync-x
[ "$brc" != 0 ] && [ ! -e "$STUB/addon.argv" ] || fail "connect-cli without a password must not call addon.sh"
# Two budgets and no sync id: refused.
{ budget_entry sync-a Household; printf ','; budget_entry sync-b Business; } > "$STUB/files.json"
ACTUAL_PASSWORD=cli-SECRET cli_run --slug home --url https://budget.example.com
[ "$brc" != 0 ] && [ ! -e "$STUB/addon.argv" ] || fail "connect-cli with two budgets must not call addon.sh"
grep -q 'SYNC_ID' "$tmp/jerr" || fail "connect-cli names SYNC_ID when there are several budgets"
cli_run --slug home
[ "$brc" != 0 ] || fail "connect-cli without --url must fail"
# addon.sh refuses: its message on stderr as a [budget] line, its exit code.
printf '{"phase":"error","error":"exists","step":null,"message":"actual already has an instance home","logTail":[]}\n' > "$STUB/addon.out"
printf 'error: actual already has an instance home\n' > "$STUB/addon.err"
printf '2' > "$STUB/addon.rc"
cli_run --slug home --url local --sync-id sync-l
[ "$brc" = 2 ] && [ ! -s "$tmp/jout" ] || fail "connect-cli passes addon.sh's exit code on ($brc, $(cat "$tmp/jout"))"
[ "$(cat "$tmp/jerr")" = '[budget] ERROR: actual already has an instance home (exists)' ] || fail "connect-cli prints addon.sh's error once ($(cat "$tmp/jerr"))"
rm -f "$STUB/addon.out" "$STUB/addon.err" "$STUB/addon.rc"

# --- disconnect-cli (make budget-disconnect) ---
dcli_run() { # ARGS...
  brc=0
  rm -f "$STUB/addon.argv"
  STACK_PYTHON=host BUDGET_DATA_ROOT="$tmp/data" BUDGET_ADDON_SH="$tmp/bin/addon-stub" \
    "$budget" disconnect-cli "$@" < /dev/null > "$tmp/jout" 2> "$tmp/jerr" || brc=$?
  cat "$tmp/jout" "$tmp/jerr" >> "$all"
}
printf '{"phase":"setup","step":"reload","status":"done"}\n{"phase":"done","id":"actual","action":"disconnect","restart":[],"mcp":[]}\n' > "$STUB/addon.out"
dcli_run --slug home
[ "$brc" = 0 ] && [ "$(cat "$tmp/jout")" = '[budget] Disconnected home' ] || fail "disconnect-cli prints a [budget] line ($brc, $(cat "$tmp/jout" "$tmp/jerr"))"
grep -qx 'disconnect actual --instance home --json' "$STUB/addon.argv" || fail "disconnect-cli calls addon.sh disconnect ($(cat "$STUB/addon.argv"))"
printf '{"phase":"error","error":"not_found","step":null,"message":"actual has no instance nope","logTail":[]}\n' > "$STUB/addon.out"
printf 'error: actual has no instance nope\n' > "$STUB/addon.err"
printf '2' > "$STUB/addon.rc"
dcli_run --slug nope
[ "$brc" = 2 ] && [ ! -s "$tmp/jout" ] && [ "$(cat "$tmp/jerr")" = '[budget] ERROR: actual has no instance nope (not_found)' ] || fail "disconnect-cli error ($brc, $(cat "$tmp/jout" "$tmp/jerr"))"
# No JSON from addon.sh (a usage error): its stderr is shown.
rm -f "$STUB/addon.out"
printf 'usage: addon.sh ...\n' > "$STUB/addon.err"
dcli_run --slug home
[ "$brc" = 2 ] || fail "disconnect-cli passes the exit code on when addon.sh printed no JSON ($brc)"
grep -q 'usage: addon.sh' "$tmp/jerr" || fail "disconnect-cli shows addon.sh's stderr when it printed no JSON ($(cat "$tmp/jerr"))"
rm -f "$STUB/addon.out" "$STUB/addon.err" "$STUB/addon.rc"
dcli_run
[ "$brc" != 0 ] && [ ! -e "$STUB/addon.argv" ] || fail "disconnect-cli without --slug must fail"
reset

run frobnicate && fail "unknown subcommand must fail"

# --- No password in any output, or in curl or docker argv ---
for s in cli-SECRET 'cli-enc"SECRET' right-SECRET wrong-SECRET enc-SECRET srv-SECRET open-SECRET $pws; do
  for log in "$all" "$STUB/curl.argv" "$STUB/docker.argv"; do
    if grep -qF -- "$s" "$log"; then fail "${log##*/} contains a password"; fi
  done
done

printf 'OK budget.sh\n'
