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

# docker: down unless $STUB_HEALTH is set. Then `compose ps -q SVC` prints an
# id for each SVC=STATE word in it, and inspect prints that STATE.
cat > "$tmp/bin/docker" <<'SH'
#!/usr/bin/env bash
[[ -n "${STUB_HEALTH:-}" ]] || exit 1
case "$*" in
  info) exit 0 ;;
  "compose ps -q "*)
    for w in $STUB_HEALTH; do [[ "${w%%=*}" == "${*: -1}" ]] && echo "cid-${w%%=*}"; done
    exit 0 ;;
  "inspect "*)
    for w in $STUB_HEALTH; do [[ "cid-${w%%=*}" == "${*: -1}" ]] && echo "${w#*=}"; done
    exit 0 ;;
esac
exit 1
SH
# curl: argv holds no key; prints the status in $STUB_HTTP (default 200), and
# writes $STUB_BODY to -o's file for a tools/list call. A URL on
# slow.example times out: 000 and exit 28, as curl -m does.
cat > "$tmp/bin/curl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_LOG"
if [[ "$*" == *slow.example* ]]; then echo 000; exit 28; fi
prev=""
for a in "$@"; do
  if [[ "$prev" == -o && "$a" != /dev/null && -n "${STUB_BODY:-}" ]]; then printf '%s' "$STUB_BODY" > "$a"; fi
  prev="$a"
done
echo "${STUB_HTTP:-200}"
SH
export STUB_LOG="$tmp/curl.log"
chmod +x "$tmp/bin/"*
export PATH="$tmp/bin:$PATH"

cat > "$tmp/harness.sh" <<'SH'
set -euo pipefail
# shellcheck source=lib/doctor-kit.sh
source "${ROOT}/scripts/lib/doctor-kit.sh"
# shellcheck source=lib/doctor-twenty.sh
source "${ROOT}/scripts/lib/doctor-twenty.sh"
doctor_twenty
# Nothing the block uses leaks into doctor.sh.
for v in count missing tools listed body lcode key hdr code probe base; do
  [[ -z "${!v+x}" ]] || echo "LEAKED $v"
done
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
grep -qF 'https://crm.example.com/mcp?mode=meta' "$STUB_LOG" || fail "the probe uses the meta-mode URL Hermes registers"
has "unreadable tool list warns" "crm-main: couldn't list its tools"
CATALOG='{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"get_tool_catalog"},{"name":"learn_tools"},{"name":"execute_tool"},{"name":"list_skills"}]}}'
STUB_BODY="$CATALOG" doctor
has "tool count" "crm-main: Twenty lists 4 tools, the catalog tools among them"
hasnt "no failure with the catalog" " FAIL "
[[ "$(grep -c 'crm-main: Twenty lists' "$out")" == 1 ]] || fail "one tools/list per workspace ($(cat "$out"))"
STUB_BODY='{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"list_skills"},{"name":"find_many_people"}]}}' doctor
has "missing catalog" "crm-main: Twenty lists 2 tools but not get_tool_catalog learn_tools execute_tool"

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

# An endpoint instance has no url: doctor names its endpoint and probes nothing.
setenv
rm -f "$store/instances/"*.json
printf '{\n  "endpoint": "http://host.docker.internal:45123",\n  "access": "read",\n  "trust": "untrusted"\n}\n' > "$store/instances/shared-a.json"
setkeys shared-a "$KEY_RO" ""
: > "$STUB_LOG"
doctor
has "endpoint instance is reported as shared" "crm-shared-a: shared instance at http://host.docker.internal:45123"
hasnt "no probe warning for an endpoint instance" "no answer from"
[[ ! -s "$STUB_LOG" ]] || fail "an endpoint instance is not probed ($(cat "$STUB_LOG"))"
rm -f "$store/instances/shared-a.json" "$store/secrets/shared-a.key_ro"
instance main write https://crm.example.com
setkeys main "$KEY_RO" "$KEY_RW"

# The store comes from doctor's DATA_ROOT: a ~/ root in .env is under HOME,
# and an exported ASSISTANT_DATA_ROOT wins over .env's.
printf 'COMPOSE_PROFILES=core,crm-mcp\nTWENTY_MCP_ENABLED=1\nASSISTANT_DATA_ROOT=~/data\n' > "$tmp/work/.env"
(cd "$tmp/work" && HOME="$tmp" ROOT="$ROOT" bash "$tmp/harness.sh") > "$out" 2>&1 || fail "~ root: non-zero ($(cat "$out"))"
hasnt "a ~/ root finds the store under HOME" "no workspaces in"
printf 'COMPOSE_PROFILES=core,crm-mcp\nTWENTY_MCP_ENABLED=1\nASSISTANT_DATA_ROOT=%s\n' "$tmp/elsewhere" > "$tmp/work/.env"
(cd "$tmp/work" && ASSISTANT_DATA_ROOT="$tmp/data" ROOT="$ROOT" bash "$tmp/harness.sh") > "$out" 2>&1 || fail "exported root: non-zero ($(cat "$out"))"
hasnt "an exported ASSISTANT_DATA_ROOT wins over .env" "no workspaces in"

# The whole block, line for line: the local containers' health, a workspace
# that answers with the catalog, one that times out, and one whose tool list
# is unreadable.
rm -f "$store/instances/"* "$store/secrets/"*
printf 'COMPOSE_PROFILES=core,crm\nTWENTY_MCP_ENABLED=1\nTWENTY_LOCAL_ENABLED=1\nTWENTY_ENCRYPTION_KEY=x\nASSISTANT_DATA_ROOT=%s\n' "$tmp/data" > "$tmp/work/.env"
instance main write https://crm.example.com
setkeys main "$KEY_RO" "$KEY_RW"
instance slow read https://slow.example.com/mcp
setkeys slow "$KEY_RO" ""
instance zed read https://zed.example.com
setkeys zed "$KEY_RO" ""
STUB_HEALTH="twenty-server=healthy twenty-worker=starting twenty-db=exited" STUB_BODY="$CATALOG" doctor
doctor_body="$(cat "$out")"
STUB_BODY='{"jsonrpc":"2.0","id":2,"error":{"message":"stub failure"}}' doctor
doctor_body="${doctor_body}
$(grep 'crm-zed' "$out")"
expected="
--- CRM (Twenty) ---
  OK  COMPOSE_PROFILES includes crm
  OK  TWENTY_ENCRYPTION_KEY is set
 FAIL TWENTY_DB_PASSWORD is unset — run ./scripts/setup.sh
  OK  twenty-server is healthy
 WARN twenty-worker is starting (the first start runs Twenty's migrations)
 FAIL twenty-db is exited — docker compose logs twenty-db
 WARN twenty-redis not running — make up
 WARN no local workspace yet — after make up, run make crm-init
  OK  TWENTY_MCP_ENABLED=1
  OK  crm-main: access write, keys present: ro rw
  OK  crm-main: MCP answers with the rw key (https://crm.example.com/mcp?mode=meta)
  OK  crm-main: Twenty lists 4 tools, the catalog tools among them
  OK  crm-main: MCP answers with the ro key (https://crm.example.com/mcp?mode=meta)
  OK  crm-slow: access read, keys present: ro
 WARN crm-slow: no answer from https://slow.example.com/mcp?mode=meta
  OK  crm-zed: access read, keys present: ro
  OK  crm-zed: MCP answers with the ro key (https://zed.example.com/mcp?mode=meta)
  OK  crm-zed: Twenty lists 4 tools, the catalog tools among them
  OK  crm-zed: access read, keys present: ro
  OK  crm-zed: MCP answers with the ro key (https://zed.example.com/mcp?mode=meta)
 WARN crm-zed: couldn't list its tools: tools/list failed: stub failure"
[[ "$doctor_body" == "$expected" ]] || fail "the block's output changed: $(diff <(printf '%s\n' "$expected") <(printf '%s\n' "$doctor_body"))"

if grep -q LEAKED "$all"; then fail "the block leaks variables: $(grep LEAKED "$all" | sort -u)"; fi
for k in "$KEY_RO" "$KEY_RW"; do
  if grep -qF -- "$k" "$all"; then fail "a key shows in the output"; fi
done
echo ok
