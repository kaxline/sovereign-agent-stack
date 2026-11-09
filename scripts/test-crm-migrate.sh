#!/usr/bin/env bash
# crm.sh migrate: compose/twenty/workspaces/<slug>.env files into the Twenty
# add-on's instance store. Synthetic workspaces in a scratch data root, host
# python3 (STACK_PYTHON=host), no Docker.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
crm="${ROOT}/scripts/crm.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

export STACK_PYTHON=host
export ENV_FILE="$tmp/.env"
export CRM_WORKSPACES_DIR="$tmp/ws"
export ASSISTANT_DATA_ROOT="$tmp/data"
mkdir -p "$CRM_WORKSPACES_DIR" "$ASSISTANT_DATA_ROOT"
printf 'TWENTY_MULTIWORKSPACE=0\nTWENTY_PORT=3000\n' > "$ENV_FILE"
store="$ASSISTANT_DATA_ROOT/addons/twenty"
ws="$CRM_WORKSPACES_DIR"

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}
out="$tmp/out"
err="$tmp/err"
run() {
  "$crm" "$@" < /dev/null > "$out" 2> "$err"
}
no_secret() {
  if grep -q 'SECRET' "$out" "$err"; then fail "$1: output contains a key"; fi
}
jget() {
  python3 -I -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print(json.dumps(v))' "$1" "$2"
}
mode() {
  stat -f %Lp "$1" 2>/dev/null || stat -c %a "$1"
}
mtimes() {
  python3 -I -c '
import os, sys
for top in sys.argv[1:]:
    for d, _, files in sorted(os.walk(top)):
        for f in sorted(files):
            p = os.path.join(d, f)
            print(p, os.stat(p).st_mtime_ns)
' "$store" "$ws"
}

cat > "$ws/main.env" <<'EOF'
# Written by make crm-init. Hermes registers it as mcp_servers.crm-main. See docs/crm.md.
TWENTY_URL=http://twenty-server:3000
TWENTY_WORKSPACE_URL=http://localhost:3000/
TWENTY_WORKSPACE_ID=ws-main-id
TWENTY_KEY_RO=main-ro-SECRET
TWENTY_KEY_RW=main-rw-SECRET
EOF
cat > "$ws/sales-eu.env" <<'EOF'
TWENTY_URL=https://crm.example.com/mcp
TWENTY_WORKSPACE_URL=https://acme.crm.example.com/
TWENTY_ACCESS=read
TWENTY_KEY_RW=
TWENTY_KEY_RO=sales-ro-SECRET
EOF
printf 'TWENTY_URL=https://crm.example.com\nTWENTY_KEY_RW=bad-SECRET\n' > "$ws/bad_slug!.env"
chmod 600 "$ws"/*.env

run migrate || fail "migrate ($(cat "$out" "$err"))"
no_secret migrate
m="$store/instances/main.json"
[ -f "$m" ] || fail "migrate writes instances/main.json"
[ "$(jget "$m" url)" = '"http://twenty-server:3000"' ] || fail "main url"
[ "$(jget "$m" local)" = true ] || fail "main is local"
[ "$(jget "$m" access)" = '"write"' ] || fail "a file without TWENTY_ACCESS migrates as write"
[ "$(jget "$m" workspace_id)" = '"ws-main-id"' ] || fail "main workspace_id"
[ "$(jget "$m" workspace_url)" = '"http://localhost:3000/"' ] || fail "main workspace_url"
grep -q SECRET "$m" && fail "instances/main.json holds a key"
[ "$(cat "$store/secrets/main.key_ro")" = main-ro-SECRET ] || fail "main.key_ro"
[ "$(cat "$store/secrets/main.key_rw")" = main-rw-SECRET ] || fail "main.key_rw"
[ "$(mode "$store/secrets/main.key_ro")" = 600 ] || fail "main.key_ro is 0600"
[ "$(mode "$store/secrets/main.key_rw")" = 600 ] || fail "main.key_rw is 0600"

s="$store/instances/sales-eu.json"
[ -f "$s" ] || fail "migrate writes instances/sales-eu.json"
[ "$(jget "$s" url)" = '"https://crm.example.com"' ] || fail "sales-eu url drops /mcp"
[ "$(jget "$s" local)" = false ] || fail "sales-eu is not local"
[ "$(jget "$s" access)" = '"read"' ] || fail "sales-eu keeps TWENTY_ACCESS=read"
[ "$(cat "$store/secrets/sales-eu.key_ro")" = sales-ro-SECRET ] || fail "sales-eu.key_ro"
[ ! -e "$store/secrets/sales-eu.key_rw" ] || fail "an empty key writes no secret file"

[ -f "$ws/main.env.migrated" ] && [ ! -e "$ws/main.env" ] || fail "main.env renamed .env.migrated"
[ -f "$ws/sales-eu.env.migrated" ] && [ ! -e "$ws/sales-eu.env" ] || fail "sales-eu.env renamed"
[ -f "$ws/bad_slug!.env" ] || fail "a bad slug's file is left"
grep -q 'bad_slug!' "$err" || fail "the bad file is named on stderr"
[ ! -e "$store/instances/bad_slug!.json" ] || fail "a bad slug is not imported"

# A second run changes nothing.
before="$(mtimes)"
sleep 1
run migrate || fail "second migrate ($(cat "$out" "$err"))"
[ "$(mtimes)" = "$before" ] || fail "a second migrate changes files"
no_secret "second migrate"
rm "$ws/bad_slug!.env"

# A slug in both the store and a leftover file: store wins, the file stays, with a warning.
printf 'TWENTY_URL=https://other.example.com\nTWENTY_ACCESS=write\nTWENTY_KEY_RW=other-rw-SECRET\n' > "$ws/main.env"
run migrate || fail "migrate with a leftover file ($(cat "$out" "$err"))"
no_secret leftover
[ -f "$ws/main.env" ] || fail "a leftover file for a stored slug stays"
grep -q 'main' "$err" || fail "the leftover file gets a warning"
[ "$(jget "$m" url)" = '"http://twenty-server:3000"' ] || fail "the store keeps its url"
[ "$(cat "$store/secrets/main.key_rw")" = main-rw-SECRET ] || fail "the store keeps its key"
rm "$ws/main.env"

# A write failure halfway leaves the file in place; the next run retries.
# addon_store chmods its directories back to 0700 before writing, so a
# read-only secrets/ wouldn't fail; a directory where the key file goes does,
# after instances/half.json is written.
printf 'TWENTY_URL=https://half.example.com\nTWENTY_KEY_RW=half-rw-SECRET\n' > "$ws/half.env"
mkdir "$store/secrets/half.key_rw"
if run migrate; then fail "migrate with an unwritable key file must fail"; fi
rmdir "$store/secrets/half.key_rw"
grep -q 'half' "$err" || fail "a failed write names the workspace"
no_secret "failed write"
[ -f "$ws/half.env" ] || fail "a failed write leaves the file"
[ ! -e "$ws/half.env.migrated" ] || fail "a failed write renames nothing"
[ ! -e "$store/instances/half.json" ] || fail "a failed write leaves no half instance"
run migrate || fail "migrate retry ($(cat "$out" "$err"))"
[ -f "$ws/half.env.migrated" ] || fail "the retry migrates the file"
[ "$(cat "$store/secrets/half.key_rw")" = half-rw-SECRET ] || fail "the retry stores the key"

# A key with whitespace or a control character, or a URL with user:password@,
# is skipped and left in place; single-quoted values lose their quotes.
printf 'TWENTY_URL=https://crm.example.com\nTWENTY_KEY_RW=sp ace-SECRET\n' > "$ws/spacey.env"
printf 'TWENTY_URL=https://crm.example.com\nTWENTY_KEY_RW=tab\tbed-SECRET\n' > "$ws/tabby.env"
printf 'TWENTY_URL=https://u:pw-SECRET@crm.example.com\nTWENTY_KEY_RW=k-SECRET\n' > "$ws/creds.env"
printf "TWENTY_URL='https://quoted.example.com/'\nTWENTY_KEY_RW='q-SECRET'\n" > "$ws/quoted.env"
run migrate || fail "migrate with refused files ($(cat "$out" "$err"))"
no_secret "refused files"
for slug in spacey tabby creds; do
  [ -f "$ws/${slug}.env" ] || fail "${slug}.env is left in place"
  [ ! -e "$store/instances/${slug}.json" ] || fail "${slug} is not imported"
  grep -q "${slug}.env" "$err" || fail "${slug}.env is named on stderr"
done
[ "$(jget "$store/instances/quoted.json" url)" = '"https://quoted.example.com"' ] || fail "single quotes are stripped from the URL"
[ "$(cat "$store/secrets/quoted.key_rw")" = q-SECRET ] || fail "single quotes are stripped from the key"
rm "$ws/spacey.env" "$ws/tabby.env" "$ws/creds.env"

# No files: no container (a docker that fails loudly), exit 0.
empty="$tmp/empty"
mkdir -p "$empty/bin" "$empty/ws"
printf '#!/bin/sh\necho docker-called >&2\nexit 9\n' > "$empty/bin/docker"
chmod +x "$empty/bin/docker"
PATH="$empty/bin:$PATH" STACK_PYTHON='' CRM_WORKSPACES_DIR="$empty/ws" run migrate \
  || fail "migrate with no files ($(cat "$out" "$err"))"
grep -q docker-called "$err" && fail "migrate with no files starts a container"
rmdir "$empty/ws"
PATH="$empty/bin:$PATH" STACK_PYTHON='' CRM_WORKSPACES_DIR="$empty/ws" run migrate \
  || fail "migrate with no workspaces dir ($(cat "$out" "$err"))"

printf 'OK crm.sh migrate\n'
