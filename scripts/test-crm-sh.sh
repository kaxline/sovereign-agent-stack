#!/usr/bin/env bash
# scripts/crm.sh subcommands that need no running stack: add (remote
# workspaces), mode, and argument checks. Uses a temp .env and workspaces dir.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
crm="${ROOT}/scripts/crm.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

export ENV_FILE="$tmp/.env"
export CRM_WORKSPACES_DIR="$tmp/ws"
mkdir -p "$CRM_WORKSPACES_DIR"
printf 'TWENTY_MULTIWORKSPACE=0\nTWENTY_MULTIWORKSPACE_BOOL=false\nTWENTY_PORT=3000\n' > "$ENV_FILE"

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

out="$tmp/out"
run() {
  "$crm" "$@" < /dev/null > "$out" 2>&1
}
no_secret() {
  if grep -q 'SECRET' "$out"; then fail "$1: output contains a key"; fi
}

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
f="$CRM_WORKSPACES_DIR/hosted.env"
[ -f "$f" ] || fail "add writes hosted.env"
grep -qx "TWENTY_URL=$URL" "$f" || fail "add stores the URL without a trailing slash"
grep -qx "TWENTY_WORKSPACE_URL=$URL/" "$f" || fail "add defaults the workspace URL to the instance"
grep -qx 'TWENTY_KEY_RW=good-rw-SECRET' "$f" || fail "add stores the read/write key"
grep -qx 'TWENTY_KEY_RO=good-ro-SECRET' "$f" || fail "add stores the read-only key"
grep -qx 'TWENTY_ACCESS=write' "$f" || fail "add stores TWENTY_ACCESS=write by default"
[ "$(stat -f %Lp "$f" 2>/dev/null || stat -c %a "$f")" = 600 ] || fail "workspace file is mode 600"

# A key Twenty rejects: nothing written, and the error names the key.
if TWENTY_KEY_RW=good-rw-SECRET TWENTY_KEY_RO=bad-ro-SECRET run add --slug rejected --url "$URL"; then
  fail "a rejected read-only key must fail"
fi
grep -q 'TWENTY_KEY_RO' "$out" || fail "the error names the rejected key"
no_secret "rejected key"
[ ! -e "$CRM_WORKSPACES_DIR/rejected.env" ] || fail "a rejected key writes nothing"
if TWENTY_KEY_RW=good-x run add --slug down --url http://127.0.0.1:9; then fail "an unreachable URL must fail"; fi
[ ! -e "$CRM_WORKSPACES_DIR/down.env" ] || fail "an unreachable URL writes nothing"

# TWENTY_ACCESS=read needs only the read-only key; the workspace URL can be given.
TWENTY_ACCESS=read TWENTY_KEY_RO=good-ro-SECRET TWENTY_WORKSPACE_URL=https://acme.crm.example.com/ \
  run add --slug lookup --url "$URL" || fail "read access with only a read-only key ($(cat "$out"))"
grep -qx 'TWENTY_ACCESS=read' "$CRM_WORKSPACES_DIR/lookup.env" || fail "add stores TWENTY_ACCESS=read"
grep -qx 'TWENTY_WORKSPACE_URL=https://acme.crm.example.com/' "$CRM_WORKSPACES_DIR/lookup.env" \
  || fail "add stores the given workspace URL"
if TWENTY_ACCESS=read TWENTY_KEY_RW=good-rw run add --slug noro --url "$URL"; then
  fail "read access without a read-only key must fail"
fi
if TWENTY_ACCESS=admin TWENTY_KEY_RW=good-rw run add --slug odd --url "$URL"; then fail "TWENTY_ACCESS=admin accepted"; fi

# add refuses to overwrite, bad slugs, bad URLs, and a missing key with no terminal.
if TWENTY_KEY_RW=good-x run add --slug hosted --url "$URL"; then fail "add must not overwrite"; fi
grep -q -- '--force' "$out" || fail "the refusal mentions --force"
TWENTY_KEY_RW=good-new-SECRET run add --slug hosted --url "$URL" --force || fail "--force overwrites"
grep -qx 'TWENTY_KEY_RW=good-new-SECRET' "$f" || fail "--force writes the new key"
if ! grep -q '^TWENTY_KEY_RO=$' "$f"; then fail "an omitted read-only key is stored empty"; fi
if TWENTY_KEY_RW=good-x run add --slug Bad_1 --url "$URL"; then fail "bad slug accepted"; fi
# bash 3.2 bracket ranges follow the locale's collation, where [a-z] matches A-Y.
for slug in Sales SALES; do
  if LC_ALL=en_US.UTF-8 TWENTY_KEY_RW=good-x run add --slug "$slug" --url "$URL"; then
    fail "uppercase slug ${slug} accepted"
  fi
done
if TWENTY_KEY_RW=good-x run add --slug ok --url crm.example.com; then fail "URL without http(s) accepted"; fi
if run add --slug nokey --url "$URL"; then fail "no key and no terminal must fail"; fi
[ ! -e "$CRM_WORKSPACES_DIR/nokey.env" ] || fail "a failed add writes nothing"

# access: sets TWENTY_ACCESS when the file holds the key it calls for.
# hosted has an RW key and an empty RO key; lookup has only an RO key.
run access --slug lookup write && fail "write access without TWENTY_KEY_RW must fail"
grep -qx 'TWENTY_ACCESS=read' "$CRM_WORKSPACES_DIR/lookup.env" || fail "a refused access change leaves the file alone"
run access --slug hosted read && fail "read access without TWENTY_KEY_RO must fail"
run access --slug lookup read || fail "access read ($(cat "$out"))"
printf 'TWENTY_URL=http://twenty-server:3000\nTWENTY_KEY_RW=rw-SECRET\nTWENTY_KEY_RO=ro-SECRET\n' > "$tmp/both.env"
cp "$tmp/both.env" "$CRM_WORKSPACES_DIR/both.env"
chmod 644 "$CRM_WORKSPACES_DIR/both.env"
run access --slug both read || fail "access read adds the line ($(cat "$out"))"
no_secret access
grep -qx 'TWENTY_ACCESS=read' "$CRM_WORKSPACES_DIR/both.env" || fail "access read adds TWENTY_ACCESS=read"
run access --slug both write || fail "access write ($(cat "$out"))"
[ "$(grep -c '^TWENTY_ACCESS=' "$CRM_WORKSPACES_DIR/both.env")" = 1 ] || fail "access replaces the line, not adds one"
grep -qx 'TWENTY_ACCESS=write' "$CRM_WORKSPACES_DIR/both.env" || fail "access write sets TWENTY_ACCESS=write"
grep -qx 'TWENTY_KEY_RO=ro-SECRET' "$CRM_WORKSPACES_DIR/both.env" || fail "access keeps the keys"
[ "$(stat -f %Lp "$CRM_WORKSPACES_DIR/both.env" 2>/dev/null || stat -c %a "$CRM_WORKSPACES_DIR/both.env")" = 600 ] \
  || fail "access leaves the file mode 600"
grep -q 'hermes-reload' "$out" || fail "access says to run make hermes-reload"
run access --slug both admin && fail "access takes read or write"
run access --slug missing read && fail "access on a missing workspace must fail"
run access --slug both && fail "access needs read or write"
rm "$CRM_WORKSPACES_DIR/both.env"

# login-link: refused before any container runs for remote and unknown workspaces.
"$crm" login-link --slug lookup < /dev/null > "$out" 2> "$tmp/err" && fail "login-link on a remote workspace must fail"
[ ! -s "$out" ] || fail "a refused login-link prints nothing on stdout"
grep -q 'local instance' "$tmp/err" || fail "login-link says the workspace isn't local"
run login-link --slug missing && fail "login-link on a missing workspace must fail"
run login-link --slug Bad_1 && fail "login-link checks the slug"
run login-link --frob && fail "login-link rejects unknown options"

# remove: deletes the file; unknown slugs fail.
run remove --slug lookup || fail "remove ($(cat "$out"))"
[ ! -e "$CRM_WORKSPACES_DIR/lookup.env" ] || fail "remove deletes the workspace file"
grep -q 'hermes-reload' "$out" || fail "remove says to run make hermes-reload"
if run remove --slug lookup; then fail "removing a missing workspace must fail"; fi
if run remove --slug Bad_1; then fail "remove checks the slug"; fi

# mode: flips both flags; refuses single mode with two local workspaces.
run mode 1 || fail "mode 1 ($(cat "$out"))"
grep -qx 'TWENTY_MULTIWORKSPACE=1' "$ENV_FILE" || fail "mode 1 sets TWENTY_MULTIWORKSPACE=1"
grep -qx 'TWENTY_MULTIWORKSPACE_BOOL=true' "$ENV_FILE" || fail "mode 1 sets TWENTY_MULTIWORKSPACE_BOOL=true"
printf 'TWENTY_URL=http://twenty-server:3000\nTWENTY_KEY_RW=a\n' > "$CRM_WORKSPACES_DIR/main.env"
printf 'TWENTY_URL=http://twenty-server:3000\nTWENTY_KEY_RW=b\n' > "$CRM_WORKSPACES_DIR/sales.env"
if run mode 0; then fail "mode 0 with two local workspaces must be refused"; fi
grep -qx 'TWENTY_MULTIWORKSPACE=1' "$ENV_FILE" || fail "a refused mode change leaves .env alone"
rm "$CRM_WORKSPACES_DIR/sales.env"
run mode 0 || fail "mode 0 with one local workspace (plus a remote) ($(cat "$out"))"
grep -qx 'TWENTY_MULTIWORKSPACE_BOOL=false' "$ENV_FILE" || fail "mode 0 sets TWENTY_MULTIWORKSPACE_BOOL=false"
if run mode 2; then fail "mode takes 0 or 1"; fi

if run frobnicate; then fail "unknown subcommand must fail"; fi

printf 'OK crm.sh\n'
