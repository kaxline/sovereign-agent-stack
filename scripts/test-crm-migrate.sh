#!/usr/bin/env bash
# crm.sh migrate: compose/twenty/workspaces/<slug>.env files into the Twenty
# add-on's instance store. Synthetic workspaces in a scratch data root, host
# python3 (STACK_PYTHON=host), no Docker.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
crm="${ROOT}/scripts/crm.sh"
tmp="$(mktemp -d)"
trap 'chmod -R u+rwx "$tmp" 2>/dev/null; rm -rf "$tmp"' EXIT

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

# Whitespace around a value is trimmed (keys especially), and of a repeated
# key the last line wins, as the old bootstrap read the file.
printf 'TWENTY_URL=  https://trim.example.com/ \nTWENTY_KEY_RW=trim-rw-SECRET  \t\nTWENTY_KEY_RO=\t "trim-ro-SECRET" \n' > "$ws/trim.env"
printf 'TWENTY_URL=https://first.example.com\nTWENTY_KEY_RW=first-rw-SECRET\nTWENTY_URL=https://dup.example.com\nTWENTY_KEY_RW=dup-rw-SECRET\n' > "$ws/dup.env"
run migrate || fail "migrate trimmed and repeated values ($(cat "$out" "$err"))"
no_secret "trim and dup"
[ -f "$ws/trim.env.migrated" ] || fail "a key with surrounding whitespace migrates ($(cat "$err"))"
[ "$(jget "$store/instances/trim.json" url)" = '"https://trim.example.com"' ] || fail "the URL is trimmed"
[ "$(cat "$store/secrets/trim.key_rw")" = trim-rw-SECRET ] || fail "key_rw is trimmed"
[ "$(cat "$store/secrets/trim.key_ro")" = trim-ro-SECRET ] || fail "key_ro is trimmed, then unquoted"
[ "$(jget "$store/instances/dup.json" url)" = '"https://dup.example.com"' ] || fail "the last TWENTY_URL wins"
[ "$(cat "$store/secrets/dup.key_rw")" = dup-rw-SECRET ] || fail "the last TWENTY_KEY_RW wins"

# CRLF line endings (the old bootstrap accepted them), with quoted values.
printf 'TWENTY_URL="https://crlf.example.com/"\r\nTWENTY_ACCESS=read\r\nTWENTY_KEY_RO=\x27crlf-ro-SECRET\x27\r\nTWENTY_KEY_RW="crlf-rw-SECRET"\r\n' > "$ws/crlf.env"
run migrate || fail "migrate a CRLF file ($(cat "$out" "$err"))"
no_secret "CRLF file"
[ -f "$ws/crlf.env.migrated" ] || fail "a CRLF file migrates"
c="$store/instances/crlf.json"
[ "$(jget "$c" url)" = '"https://crlf.example.com"' ] || fail "CRLF url"
[ "$(jget "$c" access)" = '"read"' ] || fail "CRLF access"
[ "$(cat "$store/secrets/crlf.key_ro")" = crlf-ro-SECRET ] || fail "CRLF key_ro"
[ "$(cat "$store/secrets/crlf.key_rw")" = crlf-rw-SECRET ] || fail "CRLF key_rw"

# init with a workspace file migrate couldn't store refuses before it reaches
# Docker, so it never creates a second, empty workspace under the same slug.
# A directory where the key file goes makes the store write fail.
blk="$tmp/blk"
mkdir -p "$blk/bin"
printf '#!/bin/sh\necho "docker $*" >> "%s/docker.log"\nexit 9\n' "$blk" > "$blk/bin/docker"
chmod +x "$blk/bin/docker"
cp "$ENV_FILE" "$blk/.env"
printf 'TWENTY_LOCAL_ENABLED=1\nTWENTY_USERNAME=owner@example.com\nTWENTY_PASSWORD=pw-SECRET-123\n' >> "$blk/.env"
printf 'TWENTY_URL=http://twenty-server:3000\nTWENTY_KEY_RO=lm-ro-SECRET\nTWENTY_KEY_RW=lm-rw-SECRET\n' > "$ws/lm.env"
mkdir "$store/secrets/lm.key_rw"
if PATH="$blk/bin:$PATH" ENV_FILE="$blk/.env" run init --slug lm; then
  fail "init with an unmigrated file for its slug must fail"
fi
no_secret "init blocked"
grep -q 'lm.env' "$err" || fail "init names the unmigrated file"
grep -q 'crm.sh migrate' "$err" || fail "init points at crm.sh migrate"
[ ! -e "$blk/docker.log" ] || fail "init with an unmigrated file reaches Docker ($(cat "$blk/docker.log"))"
[ ! -e "$store/instances/lm.json" ] || fail "init with an unmigrated file stores nothing"
# --json: the hook's error line.
if PATH="$blk/bin:$PATH" ENV_FILE="$blk/.env" run init --slug lm --json; then
  fail "init --json with an unmigrated file must fail"
fi
tail -1 "$out" | grep -q '"phase":"error","error":"command_failed"' \
  || fail "init --json reports command_failed ($(cat "$out"))"
[ ! -e "$blk/docker.log" ] || fail "init --json with an unmigrated file reaches Docker"
# Another slug's unmigrated file on the local URL blocks too: init can't see
# that workspace, so single-workspace mode would let a second one in.
if PATH="$blk/bin:$PATH" ENV_FILE="$blk/.env" run init --slug other; then
  fail "init with another unmigrated local workspace must fail"
fi
grep -q 'lm.env' "$err" || fail "init names the other unmigrated local file"
[ ! -e "$blk/docker.log" ] || fail "init with another unmigrated local file reaches Docker"
rmdir "$store/secrets/lm.key_rw"
# An unmigrated remote workspace for another slug doesn't block: init goes on
# to Docker (the stub fails it there).
mv "$ws/lm.env" "$tmp/lm.env.keep"
printf 'TWENTY_URL=https://remote.example.com\nTWENTY_KEY_RW=r-rw-SECRET\n' > "$ws/remote.env"
mkdir "$store/secrets/remote.key_rw"
if PATH="$blk/bin:$PATH" ENV_FILE="$blk/.env" run init --slug other; then
  fail "init with the docker stub must fail"
fi
grep -q 'Docker daemon' "$err" || fail "an unmigrated remote file doesn't block init ($(cat "$err"))"
rmdir "$store/secrets/remote.key_rw"
rm -f "$ws/remote.env" "$blk/docker.log" "$tmp/lm.env.keep"

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

# ensure creates the store the Hermes bootstraps mount, 0700, before Docker
# would create it root-owned.
fresh="$tmp/fresh"
mkdir -p "$fresh/ws"
ASSISTANT_DATA_ROOT="$fresh/data" CRM_WORKSPACES_DIR="$fresh/ws" run ensure \
  || fail "ensure on a fresh data root ($(cat "$out" "$err"))"
for d in "$fresh/data/addons/twenty" "$fresh/data/addons/twenty/instances" "$fresh/data/addons/twenty/secrets"; do
  [ -d "$d" ] || fail "ensure creates ${d#"$fresh"/}"
  [ "$(mode "$d")" = 700 ] || fail "${d#"$fresh"/} is 0700 ($(mode "$d"))"
done

# A store directory chmod can't change (Docker on Linux created it owned by
# root): ensure warns with the fix and goes on, so make up isn't aborted.
mkdir -p "$fresh/bin"
printf '#!/bin/sh\necho "chmod: Operation not permitted" >&2\nexit 1\n' > "$fresh/bin/chmod"
chmod +x "$fresh/bin/chmod"
PATH="$fresh/bin:$PATH" ASSISTANT_DATA_ROOT="$fresh/data" CRM_WORKSPACES_DIR="$fresh/ws" run ensure \
  || fail "ensure with a failing chmod must not abort ($(cat "$out" "$err"))"
grep -q "$fresh/data/addons/twenty" "$err" || fail "the chmod warning names the store ($(cat "$err"))"
grep -q 'chown' "$err" || fail "the chmod warning gives the chown fix"

# A store directory Docker created root-owned with no instances/ in it (0555
# here): mkdir fails, and ensure warns with the fix and still runs the rest.
if [ "$(id -u)" != 0 ]; then
  ro="$tmp/ro"
  mkdir -p "$ro/data/addons/twenty" "$ro/ws"
  chmod 555 "$ro/data/addons/twenty"
  printf 'TWENTY_MULTIWORKSPACE=1\nTWENTY_PORT=3000\n' > "$tmp/ro.env"
  ENV_FILE="$tmp/ro.env" ASSISTANT_DATA_ROOT="$ro/data" CRM_WORKSPACES_DIR="$ro/ws" run ensure \
    || fail "ensure on an unwritable store must not abort ($(cat "$out" "$err"))"
  grep -q "$ro/data/addons/twenty" "$err" || fail "the mkdir warning names the store ($(cat "$err"))"
  grep -q 'chown' "$err" || fail "the mkdir warning gives the chown fix"
  grep -q '^TWENTY_MULTIWORKSPACE_BOOL=true$' "$tmp/ro.env" || fail "ensure goes on after the mkdir warning"
  chmod 755 "$ro/data/addons/twenty"
fi

printf 'OK crm.sh migrate\n'
