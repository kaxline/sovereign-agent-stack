#!/usr/bin/env bash
# scripts/crm.sh subcommands that need no running stack: add (remote
# workspaces), access, remove, mode, the connect/disconnect hooks and argument
# checks. Uses a temp .env, workspaces dir and data root, and host python3.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
crm="${ROOT}/scripts/crm.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

export STACK_PYTHON=host
export ENV_FILE="$tmp/.env"
export CRM_WORKSPACES_DIR="$tmp/ws"
export ASSISTANT_DATA_ROOT="$tmp/data"
mkdir -p "$CRM_WORKSPACES_DIR" "$ASSISTANT_DATA_ROOT"
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
mode_of() { stat -f %Lp "$1" 2>/dev/null || stat -c %a "$1"; }

# A stub Twenty MCP: initialize answers 200 for keys starting "good-", 401 otherwise.
cat > "$tmp/stub.py" <<'PY'
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        ok = self.path == "/mcp" and self.headers.get("authorization", "").startswith("Bearer good-")
        self.send_response(200 if ok else 401)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")
    def log_message(self, *a):
        pass
s = HTTPServer(("127.0.0.1", 0), H)
print(s.server_port, flush=True)
s.serve_forever()
PY
python3 -I "$tmp/stub.py" > "$tmp/port" &
stub_pid=$!
trap 'kill "$stub_pid" 2>/dev/null; wait "$stub_pid" 2>/dev/null || true; rm -rf "$tmp"' EXIT
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$tmp/port" ] && break; sleep 0.2; done
URL="http://127.0.0.1:$(cat "$tmp/port")"

# add: values from the environment, no prompts; both keys checked first.
TWENTY_KEY_RW=good-rw-SECRET TWENTY_KEY_RO=good-ro-SECRET run add --slug hosted --url "$URL/" \
  || fail "add should succeed ($(cat "$out"))"
no_secret add
f="$(inst hosted)"
[ -f "$f" ] || fail "add stores instance hosted"
grep -q 'crm-hosted' "$out" || fail "add names the crm-hosted entry"
grep -q 'hermes-reload' "$out" || fail "add says to run make hermes-reload"
[ "$(jget "$f" url)" = "\"$URL\"" ] || fail "add stores the URL without a trailing slash"
[ "$(jget "$f" workspace_url)" = "\"$URL/\"" ] || fail "add defaults the workspace URL to the instance"
[ "$(jget "$f" access)" = '"write"' ] || fail "add stores access write by default"
[ "$(jget "$f" local)" = false ] || fail "add stores local false for a remote URL"
[ "$(secret hosted.key_rw)" = good-rw-SECRET ] || fail "add stores the read/write key"
[ "$(secret hosted.key_ro)" = good-ro-SECRET ] || fail "add stores the read-only key"
[ "$(mode_of "$store/secrets/hosted.key_rw")" = 600 ] || fail "key files are mode 600"
[ ! -e "$CRM_WORKSPACES_DIR/hosted.env" ] || fail "add writes no workspace file"

# A key Twenty rejects: nothing written, and the error names the key.
if TWENTY_KEY_RW=good-rw-SECRET TWENTY_KEY_RO=bad-ro-SECRET run add --slug rejected --url "$URL"; then
  fail "a rejected read-only key must fail"
fi
grep -q 'TWENTY_KEY_RO' "$out" || fail "the error names the rejected key"
no_secret "rejected key"
[ ! -e "$(inst rejected)" ] || fail "a rejected key writes nothing"
if TWENTY_KEY_RW=good-x run add --slug down --url http://127.0.0.1:9; then fail "an unreachable URL must fail"; fi
grep -q 'no answer from' "$out" || fail "an unreachable URL says no answer"
[ ! -e "$(inst down)" ] || fail "an unreachable URL writes nothing"

# TWENTY_ACCESS=read needs only the read-only key; the workspace URL can be given.
TWENTY_ACCESS=read TWENTY_KEY_RO=good-ro-SECRET TWENTY_WORKSPACE_URL=https://acme.crm.example.com/ \
  run add --slug lookup --url "$URL" || fail "read access with only a read-only key ($(cat "$out"))"
[ "$(jget "$(inst lookup)" access)" = '"read"' ] || fail "add stores access read"
[ "$(jget "$(inst lookup)" workspace_url)" = '"https://acme.crm.example.com/"' ] \
  || fail "add stores the given workspace URL"
[ ! -e "$store/secrets/lookup.key_rw" ] || fail "an omitted key writes no key file"
if TWENTY_ACCESS=read TWENTY_KEY_RW=good-rw run add --slug noro --url "$URL"; then
  fail "read access without a read-only key must fail"
fi
if TWENTY_ACCESS=admin TWENTY_KEY_RW=good-rw run add --slug odd --url "$URL"; then fail "TWENTY_ACCESS=admin accepted"; fi

# add refuses to overwrite, bad slugs, bad URLs, and a missing key with no terminal.
if TWENTY_KEY_RW=good-x run add --slug hosted --url "$URL"; then fail "add must not overwrite"; fi
grep -q -- '--force' "$out" || fail "the refusal mentions --force"
TWENTY_KEY_RW=good-new-SECRET run add --slug hosted --url "$URL" --force || fail "--force overwrites"
[ "$(secret hosted.key_rw)" = good-new-SECRET ] || fail "--force writes the new key"
[ ! -e "$store/secrets/hosted.key_ro" ] || fail "--force drops a read-only key it wasn't given"
# A --force whose store write fails keeps the old workspace and its keys. A
# directory where key_ro goes makes configure fail after it wrote the JSON.
mkdir "$store/secrets/hosted.key_ro"
if TWENTY_ACCESS=read TWENTY_KEY_RW=good-other-SECRET TWENTY_KEY_RO=good-ro2-SECRET \
  TWENTY_WORKSPACE_URL=https://changed.example.com/ run add --slug hosted --url "$URL" --force; then
  fail "a --force whose write fails must fail"
fi
no_secret "failed --force"
rmdir "$store/secrets/hosted.key_ro"
[ "$(jget "$f" access)" = '"write"' ] || fail "a failed --force keeps the old access"
[ "$(jget "$f" workspace_url)" = "\"$URL/\"" ] || fail "a failed --force keeps the old workspace URL"
[ "$(secret hosted.key_rw)" = good-new-SECRET ] || fail "a failed --force keeps the old key"
[ ! -e "$store/secrets/hosted.key_ro" ] || fail "a failed --force adds no key"

# Keys with whitespace or control characters and URLs with user:password@ are refused.
if TWENTY_KEY_RW='good x-SECRET' run add --slug spacey --url "$URL"; then fail "a key with a space accepted"; fi
no_secret "key with a space"
[ ! -e "$(inst spacey)" ] || fail "a key with a space writes nothing"
if TWENTY_KEY_RW="good-$(printf '\t')x" run add --slug tabby --url "$URL"; then fail "a key with a tab accepted"; fi
if TWENTY_KEY_RW=good-x run add --slug creds --url "https://u:pw-SECRET@crm.example.com"; then
  fail "a URL with user:password@ accepted"
fi
no_secret "URL with a password"
[ ! -e "$(inst creds)" ] || fail "a URL with a password writes nothing"

# remove and add --force retire a leftover workspace file, so migrate can't bring it back.
TWENTY_KEY_RW=good-rw-SECRET run add --slug spare --url "$URL" || fail "add spare ($(cat "$out"))"
printf 'TWENTY_URL=https://old.example.com\nTWENTY_KEY_RW=old-SECRET\n' > "$CRM_WORKSPACES_DIR/spare.env"
run remove --slug spare || fail "remove spare ($(cat "$out"))"
[ ! -e "$CRM_WORKSPACES_DIR/spare.env" ] || fail "remove renames a leftover workspace file"
[ -f "$CRM_WORKSPACES_DIR/spare.env.migrated" ] || fail "remove leaves the leftover as .env.migrated"
run migrate || fail "migrate after remove ($(cat "$out"))"
[ ! -e "$(inst spare)" ] || fail "migrate brings back a removed workspace"
TWENTY_KEY_RW=good-rw-SECRET run add --slug spare2 --url "$URL" || fail "add spare2 ($(cat "$out"))"
printf 'TWENTY_URL=https://old.example.com\nTWENTY_KEY_RW=old-SECRET\n' > "$CRM_WORKSPACES_DIR/spare2.env"
TWENTY_KEY_RW=good-rw2-SECRET run add --slug spare2 --url "$URL" --force || fail "add --force spare2 ($(cat "$out"))"
[ ! -e "$CRM_WORKSPACES_DIR/spare2.env" ] || fail "add --force renames a leftover workspace file"
[ "$(secret spare2.key_rw)" = good-rw2-SECRET ] || fail "add --force stores the new key over a leftover file"
run remove --slug spare2 || fail "remove spare2"
if TWENTY_KEY_RW=good-x run add --slug Bad_1 --url "$URL"; then fail "bad slug accepted"; fi
# bash 3.2 bracket ranges follow the locale's collation, where [a-z] matches A-Y.
for slug in Sales SALES; do
  if LC_ALL=en_US.UTF-8 TWENTY_KEY_RW=good-x run add --slug "$slug" --url "$URL"; then
    fail "uppercase slug ${slug} accepted"
  fi
done
if TWENTY_KEY_RW=good-x run add --slug ok --url crm.example.com; then fail "URL without http(s) accepted"; fi
if run add --slug nokey --url "$URL"; then fail "no key and no terminal must fail"; fi
[ ! -e "$(inst nokey)" ] || fail "a failed add writes nothing"

# access: sets access when the instance holds the key it calls for.
# hosted has only an RW key; lookup has only an RO key.
run access --slug lookup write && fail "write access without a read/write key must fail"
[ "$(jget "$(inst lookup)" access)" = '"read"' ] || fail "a refused access change leaves the instance alone"
run access --slug hosted read && fail "read access without a read-only key must fail"
run access --slug lookup read || fail "access read ($(cat "$out"))"
# A workspace file left from before 0.3.13 is migrated first.
printf 'TWENTY_URL=http://twenty-server:3000\nTWENTY_KEY_RW=rw-SECRET\nTWENTY_KEY_RO=ro-SECRET\n' \
  > "$CRM_WORKSPACES_DIR/both.env"
run access --slug both read || fail "access read on a migrated file ($(cat "$out"))"
no_secret access
grep -q 'Set TWENTY_ACCESS=read for both' "$out" || fail "access keeps its log line"
[ "$(jget "$(inst both)" access)" = '"read"' ] || fail "access read stores read"
[ -f "$CRM_WORKSPACES_DIR/both.env.migrated" ] || fail "access migrates the workspace file first"
run access --slug both write || fail "access write ($(cat "$out"))"
[ "$(jget "$(inst both)" access)" = '"write"' ] || fail "access write stores write"
[ "$(secret both.key_ro)" = ro-SECRET ] || fail "access keeps the keys"
grep -q 'hermes-reload' "$out" || fail "access says to run make hermes-reload"
run access --slug both admin && fail "access takes read or write"
run access --slug missing read && fail "access on a missing workspace must fail"
run access --slug both && fail "access needs read or write"

# login-link: refused before any container runs for remote and unknown workspaces.
"$crm" login-link --slug lookup < /dev/null > "$out" 2> "$tmp/err" && fail "login-link on a remote workspace must fail"
[ ! -s "$out" ] || fail "a refused login-link prints nothing on stdout"
grep -q 'local instance' "$tmp/err" || fail "login-link says the workspace isn't local"
run login-link --slug missing && fail "login-link on a missing workspace must fail"
run login-link --slug Bad_1 && fail "login-link checks the slug"
run login-link --frob && fail "login-link rejects unknown options"

# remove: deletes the instance and its keys; unknown slugs fail.
run remove --slug lookup || fail "remove ($(cat "$out"))"
[ ! -e "$(inst lookup)" ] || fail "remove deletes the instance"
[ ! -e "$store/secrets/lookup.key_ro" ] || fail "remove deletes the instance's keys"
grep -q 'hermes-reload' "$out" || fail "remove says to run make hermes-reload"
if run remove --slug lookup; then fail "removing a missing workspace must fail"; fi
if run remove --slug Bad_1; then fail "remove checks the slug"; fi
run remove --slug both || fail "remove a local workspace ($(cat "$out"))"
grep -q 'still exist in Twenty' "$out" || fail "removing a local workspace says it still exists in Twenty"

# mode: flips both flags; refuses single mode with two local workspaces.
run mode 1 || fail "mode 1 ($(cat "$out"))"
grep -qx 'TWENTY_MULTIWORKSPACE=1' "$ENV_FILE" || fail "mode 1 sets TWENTY_MULTIWORKSPACE=1"
grep -qx 'TWENTY_MULTIWORKSPACE_BOOL=true' "$ENV_FILE" || fail "mode 1 sets TWENTY_MULTIWORKSPACE_BOOL=true"
printf 'TWENTY_URL=http://twenty-server:3000\nTWENTY_KEY_RW=a\n' > "$CRM_WORKSPACES_DIR/main.env"
printf 'TWENTY_URL=http://twenty-server:3000\nTWENTY_KEY_RW=b\n' > "$CRM_WORKSPACES_DIR/sales.env"
run migrate || fail "migrate ($(cat "$out"))"
if run mode 0; then fail "mode 0 with two local workspaces must be refused"; fi
grep -qx 'TWENTY_MULTIWORKSPACE=1' "$ENV_FILE" || fail "a refused mode change leaves .env alone"
run remove --slug sales || fail "remove sales"
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
hook bad_value '{"url":"https://crm.example.com","access":"read","key_ro":"k SECRET"}' connect --slug x --json
hook bad_value '{"url":"https://crm.example.com","access":"write","key_rw":"k\tSECRET"}' connect --slug x --json
hook bad_value '{"url":"https://u:pw-SECRET@crm.example.com","access":"read","key_ro":"k-SECRET"}' connect --slug x --json
[ -s "$CURL_LOG" ] && fail "a refused key or URL reaches curl"
hook "done" '{"url":"https://crm.example.com","access":"write","key_rw":"k-SECRET","key_ro":"k2-SECRET"}' connect --slug x --json
grep -q 'Authorization: Bearer k-SECRET' "$CURL_LOG.headers" || fail "the key goes to curl in a header file"
grep -q 'https://crm.example.com/mcp' "$CURL_LOG" || fail "connect checks <url>/mcp"
[ "$(grep -c 'https://crm.example.com/mcp' "$CURL_LOG")" = 2 ] || fail "connect checks both keys"
printf '%s' "$last" | grep -q '"account": *"x"' || fail "done carries account"
printf '%s' "$last" | grep -q '"mcp": *\["crm-x"\]' || fail "done carries mcp"
ENV_FILE="$LOCAL_ON" hook "done" '{"url":"http://twenty-server:3000","local":true,"access":"read","key_ro":"k-SECRET"}' connect --slug x --json
grep -q 'http://127.0.0.1:3000/mcp' "$CURL_LOG" || fail "a local instance is checked on the published port"
[ "$(cd "$store" && find . | sort)" = "$before_store" ] || fail "connect writes nothing to the store"
[ ! -e "$CRM_WORKSPACES_DIR/x.env" ] || fail "connect writes no workspace file"

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
# after migrating a pre-0.3.13 local workspace that isn't named main: it works
# on that workspace (complete keys: nothing to do) instead of refusing main as
# a second one. A stub docker reports twenty-server healthy.
mkdir -p "$tmp/dbin" "$tmp/ws-acme" "$tmp/data-acme"
cat > "$tmp/dbin/docker" <<'EOF'
#!/bin/sh
case "$*" in
  "compose ps"*) echo stub-cid ;;
  inspect*) echo healthy ;;
esac
exit 0
EOF
chmod +x "$tmp/dbin/docker"
printf 'TWENTY_URL=http://twenty-server:3000\nTWENTY_ACCESS=write\nTWENTY_KEY_RO=ro-SECRET\nTWENTY_KEY_RW=rw-SECRET\n' > "$tmp/ws-acme/acme.env"
cp "$LOCAL_ON" "$tmp/acme.env"
printf 'TWENTY_USERNAME=owner@example.com\n' >> "$tmp/acme.env"
PATH="$tmp/dbin:$PATH" ENV_FILE="$tmp/acme.env" CRM_WORKSPACES_DIR="$tmp/ws-acme" \
  ASSISTANT_DATA_ROOT="$tmp/data-acme" hook "done" '' init --json
printf '%s' "$last" | grep -q '"account": *"acme"' || fail "init --json works on the migrated local workspace: $last"
[ -e "$tmp/data-acme/addons/twenty/instances/acme.json" ] || fail "init --json migrated acme"
[ ! -e "$tmp/data-acme/addons/twenty/instances/main.json" ] || fail "init --json made no main beside acme"
grep -q 'single-workspace mode has one' "$tmp/err" || fail "init --json says it took the stored local workspace"

# init --json's done line lists crm-<slug> for every stored instance the
# bootstrap would register: it has a key for its access, and a local one needs
# TWENTY_LOCAL_ENABLED. Stored by migrating workspace files; a stub docker
# reports twenty-server healthy, and complete keys mean init has nothing to do.
mcp_of() { printf '%s' "$last" | python3 -I -c 'import json,sys; print(",".join(json.loads(sys.stdin.read())["mcp"]))'; }
mk_ws() { # mk_ws DIR NAME URL: a workspace file with both keys
  mkdir -p "$1"
  printf 'TWENTY_URL=%s\nTWENTY_ACCESS=write\nTWENTY_KEY_RO=ro-SECRET\nTWENTY_KEY_RW=rw-SECRET\n' "$3" > "$1/$2.env"
}
mk_ws "$tmp/ws-two" main http://twenty-server:3000
mk_ws "$tmp/ws-two" hosted https://crm.example.com
d2="$tmp/data-two"
mkdir -p "$d2"
PATH="$tmp/dbin:$PATH" ENV_FILE="$tmp/acme.env" CRM_WORKSPACES_DIR="$tmp/ws-two" \
  ASSISTANT_DATA_ROOT="$d2" hook "done" '' init --json
[ "$(mcp_of)" = "crm-hosted,crm-main" ] || fail "init --json lists main and a migrated remote workspace: $last"
printf '%s' "$last" | grep -q '"account": *"main"' || fail "init --json keeps account main: $last"
# Local instance off: left out, remote ones still listed.
cp "$ENV_FILE" "$tmp/off.env"
printf 'TWENTY_LOCAL_ENABLED=0\n' >> "$tmp/off.env"
ENV_FILE="$tmp/off.env" CRM_WORKSPACES_DIR="$tmp/ws-two" ASSISTANT_DATA_ROOT="$d2" \
  hook "done" '' init --json
printf '%s' "$last" | grep -q 'local instance off' || fail "init --json with local off says so: $last"
[ "$(mcp_of)" = "crm-hosted" ] || fail "init --json with local off lists the remote instances: $last"
# A remote instance with no key file left is left out.
rm -f "$d2"/addons/twenty/secrets/hosted.key_ro "$d2"/addons/twenty/secrets/hosted.key_rw
PATH="$tmp/dbin:$PATH" ENV_FILE="$tmp/acme.env" CRM_WORKSPACES_DIR="$tmp/ws-two" \
  ASSISTANT_DATA_ROOT="$d2" hook "done" '' init --json
[ "$(mcp_of)" = "crm-main" ] || fail "init --json leaves out an instance missing its key: $last"
grep -rq SECRET "$out" "$tmp/err" && fail "init --json output holds a key"

# The plan call failing (a python3 stub): one terminal done line with an
# empty list, exit 0, a warning on stderr, no error line.
mkdir -p "$tmp/pbin"
real_py="$(command -v python3)"
printf '#!/bin/sh\ncase "$*" in *instances-plan*) echo plan-broke >&2; exit 1 ;; esac\nexec %s "$@"\n' "$real_py" > "$tmp/pbin/python3"
chmod +x "$tmp/pbin/python3"
PATH="$tmp/pbin:$tmp/dbin:$PATH" ENV_FILE="$tmp/acme.env" CRM_WORKSPACES_DIR="$tmp/ws-two" \
  ASSISTANT_DATA_ROOT="$d2" hook "done" '' init --json
[ "$(mcp_of)" = "" ] || fail "init --json with an unreadable plan lists nothing: $last"
[ "$(grep -c '"phase": *"\(done\|error\)"' "$out")" = 1 ] || fail "init --json with an unreadable plan has one terminal line: $(cat "$out")"
grep -q "couldn't work out the registered servers" "$tmp/err" || fail "an unreadable plan is warned about on stderr"
# The add-on's own flag off on the local-off path: nothing registers.
cp "$tmp/off.env" "$tmp/off-mcp.env"
printf 'TWENTY_MCP_ENABLED=0\n' >> "$tmp/off-mcp.env"
ENV_FILE="$tmp/off-mcp.env" CRM_WORKSPACES_DIR="$tmp/ws-two" ASSISTANT_DATA_ROOT="$d2" hook "done" '' init --json
[ "$(mcp_of)" = "" ] || fail "init --json with TWENTY_MCP_ENABLED=0 lists nothing: $last"
cp "$tmp/off.env" "$tmp/off-mcp1.env"
printf 'TWENTY_MCP_ENABLED=1\n' >> "$tmp/off-mcp1.env"
rm -f "$d2"/addons/twenty/secrets/hosted.key_ro
mk_ws "$tmp/ws-ro" ronly https://crm.example.com
printf 'TWENTY_URL=https://crm.example.com\nTWENTY_ACCESS=read\nTWENTY_KEY_RO=ro-SECRET\n' > "$tmp/ws-ro/ronly.env"
d3="$tmp/data-ro"; mkdir -p "$d3"
ENV_FILE="$tmp/off-mcp1.env" CRM_WORKSPACES_DIR="$tmp/ws-ro" ASSISTANT_DATA_ROOT="$d3" hook "done" '' init --json
[ "$(mcp_of)" = "crm-ronly" ] || fail "init --json lists an instance with only key_ro and access read: $last"

# Slug guard. Two local workspaces and no main, single-workspace mode: init
# without --slug keeps main, so twenty_admin.py refuses it as a third.
mk_ws "$tmp/ws-ab" a http://twenty-server:3000
mk_ws "$tmp/ws-ab" b http://twenty-server:3000
init_ab() { # init_ab ENVFILE ARGS...: init in a fresh copy of the a+b store
  local envf="$1"; shift
  rm -rf "$tmp/data-ab"; mkdir -p "$tmp/data-ab"
  PATH="$tmp/dbin:$PATH" ENV_FILE="$envf" CRM_WORKSPACES_DIR="$tmp/ws-ab" ASSISTANT_DATA_ROOT="$tmp/data-ab" \
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
cp "$tmp/acme.env" "$tmp/multi.env"
printf 'TWENTY_MULTIWORKSPACE=1\n' >> "$tmp/multi.env"
init_ab "$tmp/multi.env"
grep -q 'Using the local workspace' "$tmp/err" && fail "multi-workspace mode switched slug"
grep -q 'Single-workspace mode allows one' "$tmp/err" "$out" && fail "multi-workspace mode hit the single-workspace error"
true

if run frobnicate; then fail "unknown subcommand must fail"; fi

printf 'OK crm.sh\n'
