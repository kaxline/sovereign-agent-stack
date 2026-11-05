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

# add: values from the environment, no prompts.
TWENTY_KEY_RW=rw-SECRET TWENTY_KEY_RO=ro-SECRET run add --slug hosted --url https://crm.example.com/ \
  || fail "add should succeed ($(cat "$out"))"
no_secret add
f="$CRM_WORKSPACES_DIR/hosted.env"
[ -f "$f" ] || fail "add writes hosted.env"
grep -qx 'TWENTY_URL=https://crm.example.com' "$f" || fail "add stores the URL without a trailing slash"
grep -qx 'TWENTY_KEY_RW=rw-SECRET' "$f" || fail "add stores the read/write key"
grep -qx 'TWENTY_KEY_RO=ro-SECRET' "$f" || fail "add stores the read-only key"
[ "$(stat -f %Lp "$f" 2>/dev/null || stat -c %a "$f")" = 600 ] || fail "workspace file is mode 600"

# add refuses to overwrite, bad slugs, bad URLs, and a missing key with no terminal.
if TWENTY_KEY_RW=x run add --slug hosted --url https://crm.example.com; then fail "add must not overwrite"; fi
grep -q -- '--force' "$out" || fail "the refusal mentions --force"
TWENTY_KEY_RW=new-SECRET run add --slug hosted --url https://crm.example.com --force || fail "--force overwrites"
grep -qx 'TWENTY_KEY_RW=new-SECRET' "$f" || fail "--force writes the new key"
if ! grep -q '^TWENTY_KEY_RO=$' "$f"; then fail "an omitted read-only key is stored empty"; fi
if TWENTY_KEY_RW=x run add --slug Bad_1 --url https://crm.example.com; then fail "bad slug accepted"; fi
# bash 3.2 bracket ranges follow the locale's collation, where [a-z] matches A-Y.
for slug in Sales SALES; do
  if LC_ALL=en_US.UTF-8 TWENTY_KEY_RW=x run add --slug "$slug" --url https://crm.example.com; then
    fail "uppercase slug ${slug} accepted"
  fi
done
if TWENTY_KEY_RW=x run add --slug ok --url crm.example.com; then fail "URL without http(s) accepted"; fi
if run add --slug nokey --url https://crm.example.com; then fail "no key and no terminal must fail"; fi
[ ! -e "$CRM_WORKSPACES_DIR/nokey.env" ] || fail "a failed add writes nothing"

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
