#!/usr/bin/env bash
# scripts/crm.sh subcommands that need no running stack: add, access and
# remove against a stub addon.sh (scripts/test-twenty-addon-cli.sh runs them
# through the real one), mode, the connect/disconnect hooks and argument
# checks. Uses a temp .env and data root, and host python3.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
crm="${ROOT}/scripts/crm.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

export STACK_PYTHON=host
export ENV_FILE="$tmp/.env"
export ASSISTANT_DATA_ROOT="$tmp/data"
mkdir -p "$ASSISTANT_DATA_ROOT"
store="$ASSISTANT_DATA_ROOT/addons/twenty"
printf 'TWENTY_MULTIWORKSPACE=0\nTWENTY_MULTIWORKSPACE_BOOL=false\nTWENTY_PORT=3000\n' > "$ENV_FILE"

out="$tmp/out"
run() {
  "$crm" "$@" < /dev/null > "$out" 2>&1
}
no_secret() {
  if grep -q 'SECRET' "$out"; then fail "$1: output contains a key"; fi
}
jget() {
  python3 -I -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print(json.dumps(v))' "$1" "$2"
}
inst() { printf '%s/instances/%s.json' "$store" "$1"; }
secret() { cat "$store/secrets/$1" 2>/dev/null || true; }
mode_of() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }
# seed DATA_ROOT SLUG URL ACCESS KEY_RO KEY_RW: a stored workspace, written as
# addon_store writes one; an empty key is left out.
seed() {
  local s="$1/addons/twenty" l=false
  mkdir -p "$s/instances" "$s/secrets"
  [ "$3" = http://twenty-server:3000 ] && l=true
  printf '{"url": "%s", "access": "%s", "local": %s}\n' "$3" "$4" "$l" > "$s/instances/$2.json"
  [ -z "$5" ] || printf '%s' "$5" > "$s/secrets/$2.key_ro"
  [ -z "$6" ] || printf '%s' "$6" > "$s/secrets/$2.key_rw"
}


# add, remove and access run addon.sh (connect, disconnect, configure); a
# stub stands in for it (ADDON_SH, lib/addon-human.sh's test hook) and logs
# its argv and stdin. It prints $ADDON_OUT and exits $ADDON_RC. docker is a
# stub too, for access's Hermes reload, and so is vault.sh.
mkdir -p "$tmp/abin"
cat > "$tmp/abin/addon.sh" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$ADDON_LOG"
cat > "$ADDON_LOG.stdin"
printf '%s\n' "${ADDON_OUT:-{\"phase\":\"done\",\"mcp\":[\"crm-x\"]\}}"
exit "${ADDON_RC:-0}"
SH
cat > "$tmp/abin/docker" <<'SH'
#!/bin/sh
printf 'docker %s\n' "$*" >> "$ADDON_LOG"
exit "${DOCKER_RC:-0}"
SH
cat > "$tmp/abin/vault.sh" <<'SH'
#!/bin/sh
printf 'vault %s\n' "$*" >> "$ADDON_LOG"
exit 0
SH
chmod +x "$tmp/abin/"*
export ADDON_SH="$tmp/abin/addon.sh" VAULT_SH="$tmp/abin/vault.sh" ADDON_LOG="$tmp/addon.log"
arun() { # arun ARGS...: run with the stubs, a fresh log
  rm -f "$ADDON_LOG" "$ADDON_LOG.stdin"
  PATH="$tmp/abin:$PATH" run "$@"
}
stdin_json() { python3 -I -c 'import json,sys; v=json.load(open(sys.argv[1])); sys.exit(0 if eval(sys.argv[2]) else 1)' "$ADDON_LOG.stdin" "$1"; }

# add: values from the environment, no prompts.
ADDON_OUT='{"phase":"done","account":"hosted","mcp":["crm-hosted"]}' \
  TWENTY_KEY_RW=good-rw-SECRET TWENTY_KEY_RO=good-ro-SECRET arun add --slug hosted --url "https://crm.example.com/mcp/" \
  || fail "add should succeed ($(cat "$out"))"
no_secret add
grep -qx 'connect twenty --instance hosted --json' "$ADDON_LOG" || fail "add runs addon.sh connect ($(cat "$ADDON_LOG"))"
grep -qx '\[crm\] Connected hosted; Hermes entries: crm-hosted' "$out" || fail "add prints a [crm] line ($(cat "$out"))"
stdin_json 'v == {"url": "https://crm.example.com", "access": "write", "local": False, "workspace_url": "https://crm.example.com/", "key_ro": "good-ro-SECRET", "key_rw": "good-rw-SECRET"}' \
  || fail "add sends the workspace's JSON ($(cat "$ADDON_LOG.stdin"))"
grep -qx 'TWENTY_MCP_ENABLED=1' "$ENV_FILE" || fail "add sets TWENTY_MCP_ENABLED=1"
grep -q 'hermes-reload' "$out" || fail "add says to reload when it switched the flag on"
TWENTY_ACCESS=read TWENTY_KEY_RO='good"q\SECRET' TWENTY_WORKSPACE_URL=https://acme.crm.example.com/ \
  arun add --slug lookup --url http://twenty-server:3000 || fail "add read ($(cat "$out"))"
stdin_json 'v == {"url": "http://twenty-server:3000", "access": "read", "local": True, "workspace_url": "https://acme.crm.example.com/", "key_ro": "good\"q\\SECRET"}' \
  || fail "add escapes the JSON, sends local true for the local URL and the given workspace URL ($(cat "$ADDON_LOG.stdin"))"
grep -q 'hermes-reload' "$out" && fail "add with the flag already on says nothing about reloading"
TWENTY_KEY_RW=good-rw arun add --slug x --url https://crm.example.com --force || fail "add --force"
grep -qx 'connect twenty --instance x --force --json' "$ADDON_LOG" || fail "add --force passes --force ($(cat "$ADDON_LOG"))"
# addon.sh's error line becomes a [crm] line, and its exit code crm.sh's.
ADDON_RC=4 ADDON_OUT='{"phase":"error","error":"login_refused","message":"Twenty rejects key_rw (HTTP 401)"}' \
  TWENTY_KEY_RW=bad-SECRET arun add --slug x --url https://crm.example.com && rc=0 || rc=$?
[ "$rc" = 4 ] || fail "add exits with addon.sh's code ($rc)"
grep -qx '\[crm\] ERROR: Twenty rejects key_rw (HTTP 401) (login_refused)' "$out" || fail "add prints addon.sh's error ($(cat "$out"))"
no_secret "refused add"

# Refused before addon.sh: a stored slug without --force, no key and no
# terminal, a bad slug, no URL.
seed "$ASSISTANT_DATA_ROOT" hosted https://crm.example.com write '' rw-SECRET
if TWENTY_KEY_RW=good-x arun add --slug hosted --url https://crm.example.com; then fail "add must not overwrite"; fi
grep -q 'FORCE=1' "$out" || fail "the refusal mentions FORCE=1"
if arun add --slug nokey --url https://crm.example.com; then fail "no key and no terminal must fail"; fi
grep -q 'set TWENTY_KEY_RW' "$out" || fail "no key and no terminal names TWENTY_KEY_RW"
if TWENTY_ACCESS="read" arun add --slug nokey --url https://crm.example.com; then fail "read with no key and no terminal must fail"; fi
grep -q 'set TWENTY_KEY_RO' "$out" || fail "read with no key and no terminal names TWENTY_KEY_RO"
if TWENTY_KEY_RW=good-x arun add --slug Bad_1 --url https://crm.example.com; then fail "bad slug accepted"; fi
# bash 3.2 bracket ranges follow the locale's collation, where [a-z] matches A-Y.
for slug in Sales SALES; do
  if LC_ALL=en_US.UTF-8 TWENTY_KEY_RW=good-x arun add --slug "$slug" --url https://crm.example.com; then
    fail "uppercase slug ${slug} accepted"
  fi
done
if TWENTY_KEY_RW=good-x arun add --slug ok; then fail "add without --url accepted"; fi
[ ! -e "$ADDON_LOG" ] || fail "a refused add runs no addon.sh ($(cat "$ADDON_LOG"))"

# remove: addon.sh disconnect; the hook's message is a [crm] line.
ADDON_OUT='{"phase":"done","message":"The workspace and its keys still exist in Twenty"}' arun remove --slug hosted || fail "remove ($(cat "$out"))"
grep -qx 'disconnect twenty --instance hosted --json' "$ADDON_LOG" || fail "remove runs addon.sh disconnect ($(cat "$ADDON_LOG"))"
grep -qx '\[crm\] Disconnected hosted' "$out" || fail "remove prints a [crm] line ($(cat "$out"))"
grep -qx '\[crm\] The workspace and its keys still exist in Twenty' "$out" || fail "remove prints the hook's message"
ADDON_RC=2 ADDON_OUT='{"phase":"error","error":"not_found","message":"twenty has no instance gone"}' arun remove --slug gone && rc=0 || rc=$?
[ "$rc" = 2 ] || fail "remove of an unknown slug exits 2 ($rc)"
if arun remove; then fail "remove needs --slug"; fi

# access: addon.sh configure, then the Hermes reload; refused when the
# workspace lacks the key it calls for. hosted has only an RW key.
seed "$ASSISTANT_DATA_ROOT" lookup https://crm.example.com read ro-SECRET ''
seed "$ASSISTANT_DATA_ROOT" both http://twenty-server:3000 write ro-SECRET rw-SECRET
arun access --slug lookup write && fail "write access without a read/write key must fail"
arun access --slug hosted read && fail "read access without a read-only key must fail"
[ ! -e "$ADDON_LOG" ] || fail "a refused access runs no addon.sh"
arun access --slug both read || fail "access read ($(cat "$out"))"
no_secret access
grep -qx 'configure twenty --instance both' "$ADDON_LOG" || fail "access runs addon.sh configure ($(cat "$ADDON_LOG"))"
stdin_json 'v == {"access": "read"}' || fail "access sends the access setting"
grep -q 'Set TWENTY_ACCESS=read for both' "$out" || fail "access keeps its log line"
grep -q 'docker compose run --rm -e HERMES_BOOTSTRAP_FORCE=1 hermes-api-bootstrap' "$ADDON_LOG" || fail "access reloads Hermes"
grep -q 'vault reload-plugins' "$ADDON_LOG" || fail "access reloads the plugins"
if DOCKER_RC=1 arun access --slug both write; then fail "a failed bootstrap fails access"; fi
grep -q 'a Hermes bootstrap failed' "$out" || fail "access names the failed bootstrap"
arun access --slug both admin && fail "access takes read or write"
arun access --slug missing read && fail "access on a missing workspace must fail"
arun access --slug both && fail "access needs read or write"
rm -f "$(inst both)" "$store/secrets/both.key_ro" "$store/secrets/both.key_rw"

# login-link: refused before any container runs for remote and unknown workspaces.
"$crm" login-link --slug lookup < /dev/null > "$out" 2> "$tmp/err" && fail "login-link on a remote workspace must fail"
[ ! -s "$out" ] || fail "a refused login-link prints nothing on stdout"
grep -q 'local instance' "$tmp/err" || fail "login-link says the workspace isn't local"
run login-link --slug missing && fail "login-link on a missing workspace must fail"
run login-link --slug Bad_1 && fail "login-link checks the slug"
run login-link --frob && fail "login-link rejects unknown options"

# key: argument checks, then the refusals that need no Twenty. docker is a
# stub with no running twenty-server, so main answers local_off. The owner
# variables for another slug are never written anywhere.
seed "$ASSISTANT_DATA_ROOT" main http://twenty-server:3000 write ro-SECRET rw-SECRET
seed "$ASSISTANT_DATA_ROOT" dead http://127.0.0.1:1 write '' rw-SECRET
krun() { # krun ARGS...: stdout in $out, stderr in $tmp/err, exit code in $rc
  PATH="$tmp/abin:$PATH" "$crm" "$@" < /dev/null > "$out" 2> "$tmp/err" && rc=0 || rc=$?
}
krun key create --name Ada --access read
[ "$rc" = 4 ] || fail "key create on a stopped local instance exits 4 ($rc)"
[ "$(wc -l < "$out" | tr -d ' ')" = 1 ] || fail "key error is one stdout line ($(cat "$out"))"
python3 -I -c 'import json,sys; v=json.load(open(sys.argv[1])); sys.exit(0 if v["error"]=="local_off" and v["message"] else 1)' "$out" \
  || fail "key create says local_off ($(cat "$out"))"
krun key list --name Ada --json
[ "$rc" = 4 ] && grep -q '"local_off"' "$out" || fail "key list says local_off ($rc $(cat "$out"))"
krun key revoke --id k1
[ "$rc" = 4 ] && grep -q '"local_off"' "$out" || fail "key revoke says local_off ($rc $(cat "$out"))"
for bad in "create --access read" "create --name Ada" "create --name Ada --access admin" "revoke" "list" \
  "create --name Ada --access read --slug missing" "list --name Ada --slug Bad_1" "frob" "" "create --name Ada --access read --frob"; do
  # shellcheck disable=SC2086
  krun key $bad
  [ "$rc" = 1 ] || fail "key $bad is a usage error, exit 1 ($rc)"
  [ ! -s "$out" ] || fail "a key usage error prints nothing on stdout ($bad)"
  grep -q '^\[crm\] ERROR:' "$tmp/err" || fail "key $bad names the problem on stderr"
done
krun key list --name Ada --slug dead
[ "$rc" = 1 ] && grep -q 'TWENTY_OWNER_EMAIL' "$tmp/err" && grep -q 'TWENTY_OWNER_PASSWORD' "$tmp/err" \
  || fail "another slug without the owner variables is a usage error naming them ($rc $(cat "$tmp/err"))"
TWENTY_OWNER_EMAIL=boss@example.com TWENTY_OWNER_PASSWORD=owner-pw-SECRET krun key list --name Ada --slug dead
[ "$rc" = 4 ] && grep -q '"unreachable"' "$out" || fail "another slug with a dead URL says unreachable ($rc $(cat "$out"))"
if grep -q 'owner-pw-SECRET' "$out" "$tmp/err"; then fail "the owner password reached the output"; fi
if grep -rq 'owner-pw-SECRET' "$store" "$ENV_FILE"; then fail "the owner password was written to a file"; fi
rm -f "$(inst main)" "$(inst dead)" "$store/secrets/main.key_"* "$store/secrets/dead.key_"*

# mode: flips both flags; refuses single mode with two local workspaces.
run mode 1 || fail "mode 1 ($(cat "$out"))"
grep -qx 'TWENTY_MULTIWORKSPACE=1' "$ENV_FILE" || fail "mode 1 sets TWENTY_MULTIWORKSPACE=1"
grep -qx 'TWENTY_MULTIWORKSPACE_BOOL=true' "$ENV_FILE" || fail "mode 1 sets TWENTY_MULTIWORKSPACE_BOOL=true"
seed "$ASSISTANT_DATA_ROOT" main http://twenty-server:3000 write '' a
seed "$ASSISTANT_DATA_ROOT" sales http://twenty-server:3000 write '' b
if run mode 0; then fail "mode 0 with two local workspaces must be refused"; fi
grep -qx 'TWENTY_MULTIWORKSPACE=1' "$ENV_FILE" || fail "a refused mode change leaves .env alone"
rm -f "$(inst sales)" "$store/secrets/sales.key_rw"
run mode 0 || fail "mode 0 with one local workspace (plus a remote) ($(cat "$out"))"
grep -qx 'TWENTY_MULTIWORKSPACE_BOOL=false' "$ENV_FILE" || fail "mode 0 sets TWENTY_MULTIWORKSPACE_BOOL=false"
if run mode 2; then fail "mode takes 0 or 1"; fi

# --- connect / disconnect hooks (addon.sh calls them; they store nothing) ---
# A curl stub: answers CURL_CODE with exit CURL_EXIT, and logs its argv.
mkdir -p "$tmp/bin"
cat > "$tmp/bin/curl" <<'SH'
#!/bin/sh
printf '%s\n' "$@" >> "$CURL_LOG"
for a in "$@"; do
  case "$a" in @*) cat "${a#@}" >> "$CURL_LOG.headers" ;; esac
done
# With CURL_BODY set, a tools/list call gets it as the response body.
prev=""
for a in "$@"; do
  if [ "$prev" = -o ] && [ "$a" != /dev/null ] && [ -n "${CURL_BODY:-}" ]; then printf '%s' "$CURL_BODY" > "$a"; fi
  prev="$a"
done
printf '%s' "${CURL_CODE:-200}"
exit "${CURL_EXIT:-0}"
SH
chmod +x "$tmp/bin/curl"
export CURL_LOG="$tmp/curl.log"
before_store="$(cd "$store" && find . | sort)"
hook() { # hook EXPECTED-LAST-PHASE-OR-ERROR JSON ARGS...
  local want="$1" json="$2"
  shift 2
  : > "$CURL_LOG"
  printf '%s' "$json" | PATH="$tmp/bin:$PATH" "$crm" "$@" > "$out" 2> "$tmp/err" && rc=0 || rc=$?
  no_secret "$* ($want)"
  grep -q SECRET "$tmp/err" && fail "$* ($want): stderr holds a key"
  grep -q SECRET "$CURL_LOG" && fail "$* ($want): a key reached curl's argv"
  # Only JSON lines on stdout; the last one says how it ended.
  python3 -I -c '
import json, sys
lines = open(sys.argv[1]).read().splitlines()
assert lines, "no output"
for l in lines:
    o = json.loads(l)
    assert o["phase"] in ("setup", "done", "error"), l
for l in lines[:-1]:
    assert json.loads(l)["phase"] == "setup", l
' "$out" || fail "$* ($want): stdout is not hook JSON lines: $(cat "$out")"
  last="$(tail -1 "$out")"
  case "$want" in
    done) [ "$rc" = 0 ] || fail "$*: exit $rc for done ($(cat "$out" "$tmp/err"))"
      printf '%s' "$last" | grep -q '"phase": *"done"' || fail "$*: want done, got $last" ;;
    *) [ "$rc" != 0 ] || fail "$*: exit 0 for error $want"
      printf '%s' "$last" | grep -q "\"error\": *\"$want\"" || fail "$*: want $want, got $last" ;;
  esac
}
LOCAL_ON="$tmp/local-on.env"
cp "$ENV_FILE" "$LOCAL_ON"
printf 'TWENTY_LOCAL_ENABLED=1\n' >> "$LOCAL_ON"

hook local_mismatch '{"url":"http://twenty-server:3000","access":"read","key_ro":"k-SECRET"}' connect --slug x --json
hook local_mismatch '{"url":"https://crm.example.com","local":true,"access":"read","key_ro":"k-SECRET"}' connect --slug x --json
hook local_off '{"url":"http://twenty-server:3000","local":true,"access":"read","key_ro":"k-SECRET"}' connect --slug x --json
hook missing_key '{"url":"https://crm.example.com","access":"write","key_ro":"k-SECRET"}' connect --slug x --json
hook missing_key '{"url":"https://crm.example.com","access":"read","key_rw":"k-SECRET"}' connect --slug x --json
CURL_CODE=401 hook login_refused '{"url":"https://crm.example.com","access":"read","key_ro":"k-SECRET"}' connect --slug x --json
CURL_CODE=000 CURL_EXIT=7 hook server_unreachable '{"url":"https://crm.example.com","access":"read","key_ro":"k-SECRET"}' connect --slug x --json
CURL_CODE=403 hook no_access '{"url":"https://crm.example.com","access":"read","key_ro":"k-SECRET"}' connect --slug x --json
CURL_CODE=000 CURL_EXIT=28 hook timeout '{"url":"https://crm.example.com","access":"read","key_ro":"k-SECRET"}' connect --slug x --json
CURL_CODE=502 hook server_unreachable '{"url":"https://crm.example.com","access":"read","key_ro":"k-SECRET"}' connect --slug x --json
grep -q 502 "$out" || fail "an unexpected HTTP code is named in the message"
hook bad_slug '{"url":"https://crm.example.com","access":"read","key_ro":"k-SECRET"}' connect --slug Bad_1 --json
# Messages keep quotes and backslashes (escaped); control characters go.
# msg_is TEXT: the last line's message, decoded, is TEXT.
msg_is() {
  python3 -I -c 'import json,sys; m=json.loads(sys.argv[1])["message"]; sys.exit(0 if m == sys.argv[2] else 1)' "$last" "$1"
}
for sub in connect disconnect; do
  hook bad_slug '{"url":"https://crm.example.com","access":"read","key_ro":"k-SECRET"}' "$sub" --slug 'q"b\s' --json
  msg_is "slug 'q\"b\\s' must use lowercase letters, digits and hyphens, a letter first" \
    || fail "$sub keeps a quote and backslash in its message: $last"
done
hook bad_slug '' disconnect --slug "$(printf 'a\tb')" --json
msg_is "slug 'ab' must use lowercase letters, digits and hyphens, a letter first" || fail "a tab in a message is dropped: $last"
# Lines without quotes or backslashes are byte-for-byte as before.
hook bad_slug '' disconnect --slug Bad_1 --json
[ "$last" = '{"phase":"error","error":"bad_slug","message":"slug '\''Bad_1'\'' must use lowercase letters, digits and hyphens, a letter first"}' ] \
  || fail "bad_slug line changed: $last"
CURL_CODE=401 hook login_refused '{"url":"https://crm.example.com","access":"read","key_ro":"k-SECRET"}' connect --slug x --json
[ "$(cat "$out")" = '{"phase":"setup","status":"running","message":"key_ro: checking"}
{"phase":"error","error":"login_refused","message":"Twenty rejects key_ro (HTTP 401)"}' ] \
  || fail "login_refused lines changed: $(cat "$out")"
hook bad_value '{"url":"https://crm.example.com","access":"read","key_ro":"k SECRET"}' connect --slug x --json
hook bad_value '{"url":"https://crm.example.com","access":"write","key_rw":"k\tSECRET"}' connect --slug x --json
hook bad_value '{"url":"https://u:pw-SECRET@crm.example.com","access":"read","key_ro":"k-SECRET"}' connect --slug x --json
[ -s "$CURL_LOG" ] && fail "a refused key or URL reaches curl"
hook "done" '{"url":"https://crm.example.com","access":"write","key_rw":"k-SECRET","key_ro":"k2-SECRET"}' connect --slug x --json
grep -q 'Authorization: Bearer k-SECRET' "$CURL_LOG.headers" || fail "the key goes to curl in a header file"
grep -q 'https://crm.example.com/mcp' "$CURL_LOG" || fail "connect checks <url>/mcp"
[ "$(grep -c '"method":"initialize"' "$CURL_LOG")" = 2 ] || fail "connect checks both keys"
grep -q 'https://crm.example.com/mcp?mode=meta' "$CURL_LOG" || fail "connect checks the meta-mode URL Hermes registers"
grep -q '"method":"tools/list"' "$CURL_LOG" || fail "connect lists the tools"
grep -q "couldn't check the tools" "$tmp/err" || fail "an unreadable tool list is a warning, not a failure"
printf '%s' "$last" | grep -q '"account": *"x"' || fail "done carries account"
printf '%s' "$last" | grep -q '"mcp": *\["crm-x"\]' || fail "done carries mcp"
CATALOG='{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"get_tool_catalog"},{"name":"learn_tools"},{"name":"execute_tool"},{"name":"list_skills"}]}}'
DIRECT='{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"list_skills"},{"name":"find_many_people"},{"name":"delete_one_person"}]}}'
CURL_BODY="$CATALOG" hook "done" '{"url":"https://crm.example.com","access":"read","key_ro":"k-SECRET"}' connect --slug x --json
grep -q 'catalog tools (4 tools)' "$out" || fail "connect reports the tool count ($(cat "$out"))"
CURL_BODY="$(printf 'event: message\ndata: %s\n' "$CATALOG")" hook "done" '{"url":"https://crm.example.com","access":"read","key_ro":"k-SECRET"}' connect --slug x --json
CURL_BODY="$DIRECT" hook no_catalog '{"url":"https://crm.example.com","access":"read","key_ro":"k-SECRET"}' connect --slug x --json
grep -q 'lists 3 tools but not get_tool_catalog learn_tools execute_tool' "$out" || fail "no_catalog names the count and missing tools ($(cat "$out"))"
ENV_FILE="$LOCAL_ON" hook "done" '{"url":"http://twenty-server:3000","local":true,"access":"read","key_ro":"k-SECRET"}' connect --slug x --json
grep -q 'http://127.0.0.1:3000/mcp' "$CURL_LOG" || fail "a local instance is checked on the published port"
[ "$(cd "$store" && find . | sort)" = "$before_store" ] || fail "connect writes nothing to the store"

hook "done" '' disconnect --slug hosted --json
printf '%s' "$last" | grep -q 'still exist' && fail "a remote disconnect has no local note"
hook "done" '' disconnect --slug main --json
printf '%s' "$last" | grep -q 'still exist' || fail "a local disconnect notes the workspace still exists in Twenty"
# The store reader failing (no manifest here) doesn't fail the hook.
ADDONS_DIR="$tmp/no-addons" hook "done" '' disconnect --slug main --json
printf '%s' "$last" | grep -q 'still exist' || fail "disconnect reads local from the instance file"
hook bad_slug '' disconnect --slug Bad_1 --json
[ -e "$(inst main)" ] || fail "disconnect leaves the instance files to addon.sh"

# init --json with the local instance off: done, nothing else.
hook "done" '' init --json
printf '%s' "$last" | grep -q 'local instance off' || fail "init --json with the local instance off says so"

# init --json (no --slug, as addon.sh enable runs it) in single-workspace mode
# with a stored local workspace that isn't named main: it works
# on that workspace (complete keys: nothing to do) instead of refusing main as
# a second one. A stub docker reports twenty-server healthy.
mkdir -p "$tmp/dbin" "$tmp/data-acme"
cat > "$tmp/dbin/docker" <<'EOF'
#!/bin/sh
case "$*" in
  "compose ps"*) echo stub-cid ;;
  inspect*) echo healthy ;;
esac
exit 0
EOF
chmod +x "$tmp/dbin/docker"
seed "$tmp/data-acme" acme http://twenty-server:3000 write ro-SECRET rw-SECRET
cp "$LOCAL_ON" "$tmp/acme.env"
printf 'TWENTY_USERNAME=owner@example.com\n' >> "$tmp/acme.env"
PATH="$tmp/dbin:$PATH" ENV_FILE="$tmp/acme.env" \
  ASSISTANT_DATA_ROOT="$tmp/data-acme" hook "done" '' init --json
printf '%s' "$last" | grep -q '"account": *"acme"' || fail "init --json works on the stored local workspace: $last"
[ ! -e "$tmp/data-acme/addons/twenty/instances/main.json" ] || fail "init --json made no main beside acme"
grep -q 'single-workspace mode has one' "$tmp/err" || fail "init --json says it took the stored local workspace"

# init --json's done line lists crm-<slug> for every stored instance the
# bootstrap would register: it has a key for its access, and a local one needs
# TWENTY_LOCAL_ENABLED. Seeded into the store; a stub docker
# reports twenty-server healthy, and complete keys mean init has nothing to do.
mcp_of() { printf '%s' "$last" | python3 -I -c 'import json,sys; print(",".join(json.loads(sys.stdin.read())["mcp"]))'; }
d2="$tmp/data-two"
seed "$d2" main http://twenty-server:3000 write ro-SECRET rw-SECRET
seed "$d2" hosted https://crm.example.com write ro-SECRET rw-SECRET
PATH="$tmp/dbin:$PATH" ENV_FILE="$tmp/acme.env" \
  ASSISTANT_DATA_ROOT="$d2" hook "done" '' init --json
[ "$(mcp_of)" = "crm-hosted,crm-main" ] || fail "init --json lists main and a remote workspace: $last"
printf '%s' "$last" | grep -q '"account": *"main"' || fail "init --json keeps account main: $last"
# Local instance off: left out, remote ones still listed.
cp "$ENV_FILE" "$tmp/off.env"
printf 'TWENTY_LOCAL_ENABLED=0\n' >> "$tmp/off.env"
ENV_FILE="$tmp/off.env" ASSISTANT_DATA_ROOT="$d2" \
  hook "done" '' init --json
printf '%s' "$last" | grep -q 'local instance off' || fail "init --json with local off says so: $last"
[ "$(mcp_of)" = "crm-hosted" ] || fail "init --json with local off lists the remote instances: $last"
# A remote instance with no key file left is left out.
rm -f "$d2"/addons/twenty/secrets/hosted.key_ro "$d2"/addons/twenty/secrets/hosted.key_rw
PATH="$tmp/dbin:$PATH" ENV_FILE="$tmp/acme.env" \
  ASSISTANT_DATA_ROOT="$d2" hook "done" '' init --json
[ "$(mcp_of)" = "crm-main" ] || fail "init --json leaves out an instance missing its key: $last"
grep -rq SECRET "$out" "$tmp/err" && fail "init --json output holds a key"

# The plan call failing (a python3 stub): one terminal done line with an
# empty list, exit 0, a warning on stderr, no error line.
mkdir -p "$tmp/pbin"
real_py="$(command -v python3)"
printf '#!/bin/sh\ncase "$*" in *instances-plan*) echo plan-broke >&2; exit 1 ;; esac\nexec %s "$@"\n' "$real_py" > "$tmp/pbin/python3"
chmod +x "$tmp/pbin/python3"
PATH="$tmp/pbin:$tmp/dbin:$PATH" ENV_FILE="$tmp/acme.env" \
  ASSISTANT_DATA_ROOT="$d2" hook "done" '' init --json
[ "$(mcp_of)" = "" ] || fail "init --json with an unreadable plan lists nothing: $last"
[ "$(grep -c '"phase": *"\(done\|error\)"' "$out")" = 1 ] || fail "init --json with an unreadable plan has one terminal line: $(cat "$out")"
grep -q "couldn't work out the registered servers" "$tmp/err" || fail "an unreadable plan is warned about on stderr"
# The add-on's own flag off on the local-off path: nothing registers.
cp "$tmp/off.env" "$tmp/off-mcp.env"
printf 'TWENTY_MCP_ENABLED=0\n' >> "$tmp/off-mcp.env"
ENV_FILE="$tmp/off-mcp.env" ASSISTANT_DATA_ROOT="$d2" hook "done" '' init --json
[ "$(mcp_of)" = "" ] || fail "init --json with TWENTY_MCP_ENABLED=0 lists nothing: $last"
cp "$tmp/off.env" "$tmp/off-mcp1.env"
printf 'TWENTY_MCP_ENABLED=1\n' >> "$tmp/off-mcp1.env"
rm -f "$d2"/addons/twenty/secrets/hosted.key_ro
d3="$tmp/data-ro"
seed "$d3" ronly https://crm.example.com read ro-SECRET ''
ENV_FILE="$tmp/off-mcp1.env" ASSISTANT_DATA_ROOT="$d3" hook "done" '' init --json
[ "$(mcp_of)" = "crm-ronly" ] || fail "init --json lists an instance with only key_ro and access read: $last"

# Slug guard. Two local workspaces and no main, single-workspace mode: init
# without --slug keeps main, so twenty_admin.py refuses it as a third.
init_ab() { # init_ab ENVFILE ARGS...: init in a fresh copy of the a+b store
  local envf="$1"; shift
  rm -rf "$tmp/data-ab"
  seed "$tmp/data-ab" a http://twenty-server:3000 write ro-SECRET rw-SECRET
  seed "$tmp/data-ab" b http://twenty-server:3000 write ro-SECRET rw-SECRET
  PATH="$tmp/dbin:$PATH" ENV_FILE="$envf" ASSISTANT_DATA_ROOT="$tmp/data-ab" \
    "$crm" init "$@" < /dev/null > "$out" 2> "$tmp/err" && rc=0 || rc=$?
}
init_ab "$tmp/acme.env" --json
[ "$rc" != 0 ] || fail "init without --slug must refuse when two local workspaces and no main are stored"
grep -q 'Using the local workspace' "$tmp/err" && fail "init picked one of two local workspaces"
grep -q 'Single-workspace mode allows one' "$tmp/err" "$out" || fail "init without --slug keeps main and hits the one-workspace error"
# An explicit --slug is never replaced by the stored workspace.
init_ab "$tmp/acme.env" --slug other
grep -q 'Using the local workspace' "$tmp/err" && fail "an explicit --slug was replaced"
# Multi-workspace mode keeps main too.
# env_get reads the first matching line, so replace the flag rather than append it.
sed 's/^TWENTY_MULTIWORKSPACE=.*/TWENTY_MULTIWORKSPACE=1/' "$tmp/acme.env" > "$tmp/multi.env"
grep -qx 'TWENTY_MULTIWORKSPACE=1' "$tmp/multi.env" || fail "multi.env sets TWENTY_MULTIWORKSPACE=1"
init_ab "$tmp/multi.env"
grep -q 'Using the local workspace' "$tmp/err" && fail "multi-workspace mode switched slug"
grep -q 'Single-workspace mode allows one' "$tmp/err" "$out" && fail "multi-workspace mode hit the single-workspace error"
true

if run frobnicate; then fail "unknown subcommand must fail"; fi

printf 'OK crm.sh\n'
