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
mkdir -p "$tmp/bin" "$tmp/work" "$tmp/data/addons/twenty/instances" \
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
# setenv [LINE...]: .env with the base keys plus each LINE.
# shellcheck disable=SC2120  # some calls pass no extra lines
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

# Read as instances-plan reads the store. No access field: the manifest's
# default, read.
rm -f "$tmp/work/data/hermes/profiles/api-server/"*
printf '{\n  "url": "https://crm.example.com",\n  "local": false\n}\n' > "$store/instances/main.json"
setkeys main "$KEY_RO" ""
doctor
has "missing access is read" "crm-main: access read, keys present: ro"
hasnt "missing access is not write" " FAIL "
# An access that isn't a string.
printf '{\n  "url": "https://crm.example.com",\n  "access": ["write"]\n}\n' > "$store/instances/main.json"
doctor
has "non-string access" "crm-main: access is not a string"
# local comes from the local setting, not the URL.
setenv TWENTY_LOCAL_ENABLED=0
printf '{\n  "url": "https://crm.example.com",\n  "access": "read",\n  "local": true\n}\n' > "$store/instances/main.json"
doctor
has "local setting, remote url" "crm-main: not registered while the local instance is off"
printf '{\n  "url": "http://twenty-server:3000",\n  "access": "read",\n  "local": false\n}\n' > "$store/instances/main.json"
doctor
hasnt "local false on the local url" "not registered while the local instance is off"
has "local false on the local url is checked" "crm-main: access read, keys present: ro"
printf '{\n  "url": "https://crm.example.com",\n  "access": "read",\n  "local": "yes"\n}\n' > "$store/instances/main.json"
doctor
has "non-bool local" "crm-main: local must be true or false"
setenv
# A key file with a line break: the bootstrap refuses it, so doctor does too.
instance main read https://crm.example.com
printf '%s\n' "$KEY_RO" > "$store/secrets/main.key_ro"
doctor
has "key with a line break" "main.key_ro holds a line break or control character"
hasnt "no probe with a refused key" "MCP answers with the ro key"
setkeys main "$KEY_RO" "$KEY_RW"

for k in "$KEY_RO" "$KEY_RW"; do
  if grep -qF -- "$k" "$all"; then fail "a key shows in the output"; fi
done
echo ok
