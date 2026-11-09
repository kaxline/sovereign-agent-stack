#!/usr/bin/env bash
# make doctor's CRM (Twenty) block (scripts/lib/doctor-twenty.sh) against a
# synthetic instance store, stub docker and stub curl. No real container or
# Twenty server is used; keys are fake and must not show in any output.
#
#   bash scripts/test-twenty-doctor.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/work/compose/twenty/workspaces" "$tmp/data/addons/twenty/instances" \
  "$tmp/data/addons/twenty/secrets" "$tmp/work/data/hermes/profiles/api-server"
store="$tmp/data/addons/twenty"

fail() { printf 'FAIL %s\n' "$1" >&2; exit 1; }

cat > "$tmp/bin/docker" <<'SH'
#!/usr/bin/env bash
exit 1
SH
# curl: argv holds no key; prints the status in $STUB_HTTP (default 200).
cat > "$tmp/bin/curl" <<'SH'
#!/usr/bin/env bash
echo "${STUB_HTTP:-200}"
SH
chmod +x "$tmp/bin/"*
export PATH="$tmp/bin:$PATH"

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
# shellcheck source=lib/doctor-twenty.sh
source "${ROOT}/scripts/lib/doctor-twenty.sh"
doctor_twenty
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
setenv() {
  { printf 'COMPOSE_PROFILES=core,crm-mcp\nTWENTY_MCP_ENABLED=1\nASSISTANT_DATA_ROOT=%s\n' "$tmp/data"; printf '%s\n' "$@"; } > "$tmp/work/.env"
}
instance() { # slug access url
  printf '{\n  "url": "%s",\n  "access": "%s",\n  "local": false\n}\n' "$3" "$2" > "$store/instances/$1.json"
}
KEY_RO="fake-ro-key-0001"
KEY_RW="fake-rw-key-0002"
setkeys() { # slug ro rw (empty: none)
  rm -f "$store/secrets/$1.key_ro" "$store/secrets/$1.key_rw"
  [[ -z "$2" ]] || { printf '%s' "$2" > "$store/secrets/$1.key_ro"; chmod 600 "$store/secrets/$1.key_ro"; }
  [[ -z "$3" ]] || { printf '%s' "$3" > "$store/secrets/$1.key_rw"; chmod 600 "$store/secrets/$1.key_rw"; }
}

# No instance: a warning, no failure.
setenv
doctor
has "no instance" "no workspaces in"

# A write workspace with both keys.
instance main write https://crm.example.com
setkeys main "$KEY_RO" "$KEY_RW"
doctor
has "keys present" "crm-main: access write, keys present: ro rw"
has "probe ok" "crm-main: MCP answers with the rw key"
hasnt "no failure" " FAIL "

# Read access without a read key.
instance main read https://crm.example.com
setkeys main "" "$KEY_RW"
doctor
has "missing ro" "main.key_ro is missing"

# No keys at all.
setkeys main "" ""
doctor
has "no keys" "crm-main: no keys in"

# A rejected key.
setkeys main "$KEY_RO" "$KEY_RW"
STUB_HTTP=401 doctor
has "rejected" "Twenty rejects the ro key"

# api-server holding the read/write key.
instance main write https://crm.example.com
printf 'CRM_MAIN_KEY=%s\n' "$KEY_RW" > "$tmp/work/data/hermes/profiles/api-server/.env"
printf 'mcp_servers:\n  crm-main:\n    url: x\n' > "$tmp/work/data/hermes/profiles/api-server/config.yaml"
doctor
has "api-server rw" "api-server holds crm-main's read/write key"
printf 'CRM_MAIN_KEY=%s\n' "$KEY_RO" > "$tmp/work/data/hermes/profiles/api-server/.env"
doctor
hasnt "api-server ro" "api-server holds"
has "profile registers" "Hermes api-server profile registers crm-main"

# Legacy workspace files.
printf 'TWENTY_URL=https://crm.example.com\nTWENTY_KEY_RW=fake-legacy-key\n' > "$tmp/work/compose/twenty/workspaces/x.env"
printf 'TWENTY_URL=https://crm.example.com\n' > "$tmp/work/compose/twenty/workspaces/y.env.migrated"
doctor
has "leftover file" "compose/twenty/workspaces/x.env was not migrated"
has "names migrate" "./scripts/crm.sh migrate"
hasnt "migrated file ignored" "y.env"
printf 'TWENTY_URL=https://crm.example.com\n' > "$tmp/work/compose/twenty/workspaces/1bad.env"
printf 'TWENTY_URL=https://u:pw@crm.example.com\n' > "$tmp/work/compose/twenty/workspaces/cred.env"
printf 'TWENTY_URL=https://crm.example.com\nTWENTY_KEY_RO="fake key"\n' > "$tmp/work/compose/twenty/workspaces/space.env"
doctor
has "bad slug" "1bad.env was not migrated"
has "bad slug reason" "must start with a letter"
has "userinfo reason" "user:password@"
has "whitespace reason" "a key holds whitespace"

# Leftovers warn even with the MCP switch off.
rm -f "$tmp/work/compose/twenty/workspaces/"*.env
printf 'TWENTY_URL=https://crm.example.com\n' > "$tmp/work/compose/twenty/workspaces/x.env"
setenv
sed -i.bak 's/TWENTY_MCP_ENABLED=1/TWENTY_MCP_ENABLED=0/' "$tmp/work/.env"
doctor
has "off but leftover" "was not migrated"

for k in "$KEY_RO" "$KEY_RW" fake-legacy-key; do
  if grep -qF -- "$k" "$all"; then fail "a key shows in the output"; fi
done
echo ok
