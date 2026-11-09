#!/bin/sh
# scripts/addon.sh against the real addons/twenty manifest, in a scratch ROOT
# with its data root outside it, host python3, and stub docker, make and curl.
# enable is refused until a workspace is stored; after connect, enable runs
# crm.sh init (local instance off), which migrates a legacy workspace file;
# connect and disconnect run crm.sh's hooks (a bad value or a local mismatch
# comes back as crm.sh's error, a schema violation as addon.sh's bad_input);
# remove --delete-data deletes the instance store and the local data dir, and
# is refused while Twenty is enabled. No fake key may appear in any output or
# argv.
#
#   sh scripts/test-twenty-addon-cli.sh
#
# KEEP_TMP=1 keeps the scratch directory and prints its path.
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BASH_UNDER_TEST=bash
[ -x /bin/bash ] && BASH_UNDER_TEST=/bin/bash

# The physical path: addon.sh resolves the data root through symlinks
# (/var -> /private/var on macOS).
tmp="$(cd "$(mktemp -d)" && pwd -P)"
trap '[ -n "${KEEP_TMP:-}" ] && echo "kept $tmp" || rm -rf "$tmp"' EXIT
root="${tmp}/stack"
data="${tmp}/dataroot"
mkdir -p "${root}/scripts/lib" "${root}/compose/twenty/workspaces" "$data" "${tmp}/bin" "${tmp}/out"
# addon.sh's reload step calls vault.sh reload-plugins.
printf '#!/bin/sh\nexit 0\n' > "${root}/scripts/vault.sh"
chmod +x "${root}/scripts/vault.sh"
for f in addon.sh sync-profiles.sh crm.sh twenty_admin.py; do
  cp "${REPO}/scripts/${f}" "${root}/scripts/"
done
for f in env.sh data-root.sh python.sh addon-events.sh addon_cli.py addon_manifest.py addon_store.py; do
  cp "${REPO}/scripts/lib/${f}" "${root}/scripts/lib/"
done
cp -R "${REPO}/addons" "${root}/addons"
cp "${REPO}/docker-compose.yml" "${root}/"
# The env readers take a key's first line, so ASSISTANT_DATA_ROOT and
# TWENTY_DATA_DIR replace
# the example's ./data rather than being appended. TWENTY_LOCAL_ENABLED is 0
# in the example.
grep -v -e '^ASSISTANT_DATA_ROOT=' -e '^TWENTY_DATA_DIR=' "${REPO}/.env.example" > "${root}/.env"
{
  printf 'COMPOSE_PROJECT_NAME=twentytest\n'
  printf 'ASSISTANT_DATA_ROOT=%s\n' "$data"
  # The manifest's default ./data/twenty is relative to the repo root, so the
  # local data dir is pointed into the scratch data root.
  printf 'TWENTY_DATA_DIR=%s\n' "${data}/twenty"
} >> "${root}/.env"

# A workspace file from before 0.3.13, which enable's init hook migrates.
cat > "${root}/compose/twenty/workspaces/legacy.env" <<'EOF'
TWENTY_URL=https://legacy.example.com
TWENTY_ACCESS=read
TWENTY_KEY_RO=fake-legacy-ro-key
TWENTY_KEY_RW=fake-legacy-rw-key
EOF
KEYS="fake-legacy-ro-key fake-legacy-rw-key fake-ro-key fake-refused-key"

# Stubs log their argv, one call per line. curl prints CURL_CODE as the HTTP
# status of crm.sh's MCP initialize.
cat > "${tmp}/bin/docker" <<'EOF'
#!/bin/sh
printf 'docker %s\n' "$*" >> "${STUB_LOG}"
exit 0
EOF
cat > "${tmp}/bin/make" <<'EOF'
#!/bin/sh
printf 'make %s\n' "$*" >> "${STUB_LOG}"
exit 0
EOF
cat > "${tmp}/bin/curl" <<'EOF'
#!/bin/sh
printf 'curl %s\n' "$*" >> "${STUB_LOG}"
printf '%s' "${CURL_CODE:-200}"
exit 0
EOF
chmod +x "${tmp}/bin/docker" "${tmp}/bin/make" "${tmp}/bin/curl"
mkdir -p "${tmp}/hosttmp"
export TMPDIR="${tmp}/hosttmp"
export PATH="${tmp}/bin:${PATH}"
export STACK_PYTHON=host
export STUB_LOG="${tmp}/stub.log"
: > "$STUB_LOG"
unset ASSISTANT_DATA_ROOT ADDONS_DIR COMPOSE_PROJECT_NAME CRM_WORKSPACES_DIR ENV_FILE 2>/dev/null || true

repo_before="$(git -C "$REPO" status --porcelain 2>/dev/null || true)"
store="${data}/addons/twenty"

fail=0
n=0
ok() { echo "ok: $1"; }
bad() { echo "FAIL: $1"; fail=1; }

# addon [args...] < stdin: addon.sh with stdout in $out, stderr in $err and
# the exit code in $rc.
addon() {
  n=$((n + 1))
  out="${tmp}/out/${n}"
  err="${tmp}/out/${n}.err"
  rc=0
  "$BASH_UNDER_TEST" "${root}/scripts/addon.sh" "$@" > "$out" 2> "$err" || rc=$?
}
# addon_in JSON [args...]: addon with JSON on stdin.
addon_in() {
  printf '%s' "$1" > "${tmp}/stdin"
  shift
  addon "$@" < "${tmp}/stdin"
}
# json_ok PHASE [EXPR]: every stdout line is a JSON object, the last line's
# phase is PHASE and the Python expression EXPR holds over `lines` and `last`.
json_ok() {
  python3 -c 'import json, sys
lines = [json.loads(l) for l in open(sys.argv[1]).read().splitlines()]
assert lines and all(isinstance(l, dict) for l in lines)
last = lines[-1]
assert last["phase"] == sys.argv[2]
sys.exit(0 if eval(sys.argv[3]) else 1)' "$out" "$1" "${2:-True}" 2> /dev/null
}
show() { sed 's/^/    /' "$out"; sed 's/^/    err: /' "$err"; }
# t NAME CMD...: ok when CMD succeeds, else FAIL with the last call's output.
t() {
  name="$1"
  shift
  if "$@"; then ok "$name"; else bad "$name"; show; fi
}
expect_rc() {
  if [ "$rc" = "$2" ]; then ok "$1"; else bad "$1: exit ${rc}, want $2"; show; fi
}
# no_keys NAME: no fake key in this call's stdout or stderr.
no_keys() {
  for k in $KEYS; do
    if grep -qF -- "$k" "$out" "$err"; then bad "$1: $k in the output"; return 0; fi
  done
  ok "$1: no key in the output"
}
mode_of() { stat -f %Lp "$1" 2> /dev/null || stat -c %a "$1"; }
has_line() { grep -q "$1" "$2"; }
lacks_line() { ! grep -q "$1" "$2"; }
absent() { for p in "$@"; do [ ! -e "$p" ] || return 1; done; }
present() { for p in "$@"; do [ -f "$p" ] || return 1; done; }
json_file() { python3 -c "import json, sys; v = json.load(open(sys.argv[1])); sys.exit(0 if eval(sys.argv[2]) else 1)" "$1" "$2" 2> /dev/null; }
ws="${root}/compose/twenty/workspaces"

# --- enable before any workspace: missing_settings --------------------------------
# store.missing wants one stored instance before enable; init, which creates
# the local workspace and migrates legacy files, runs after that check.
addon enable twenty --json < /dev/null
expect_rc "enable with no workspace exits 3" 3
t "enable with no workspace is missing_settings instances:workspace" \
  json_ok error 'last.get("error") == "missing_settings" and "instances:workspace" in last.get("message", "")'
t "a refused enable leaves TWENTY_MCP_ENABLED off" lacks_line '^TWENTY_MCP_ENABLED=1$' "${root}/.env"
t "a refused enable leaves legacy.env alone" present "${ws}/legacy.env"

# --- connect sales: curl 200 ---------------------------------------------------
CURL_CODE=200
export CURL_CODE
addon_in '{"url":"https://crm.example.com","access":"read","local":false,"key_ro":"fake-ro-key"}' connect twenty --instance sales --json
expect_rc "connect exits 0" 0
t "connect ends with done, mcp [crm-sales], account sales" \
  json_ok "done" 'last.get("mcp") == ["crm-sales"] and last.get("account") == "sales"'
t "connect shows the check and register steps" \
  json_ok "done" 'any(l.get("step") == "check" and l.get("phase") == "setup" for l in lines) and any(l.get("step") == "register" for l in lines)'
no_keys "connect"
t "connect writes instances/sales.json and secrets/sales.key_ro" \
  present "${store}/instances/sales.json" "${store}/secrets/sales.key_ro"
t "secrets/sales.key_ro is 0600" test "$(mode_of "${store}/secrets/sales.key_ro")" = 600
t "connect writes no key_rw it wasn't given" absent "${store}/secrets/sales.key_rw"
t "sales.json holds the url, read access and local false" \
  json_file "${store}/instances/sales.json" 'v["url"] == "https://crm.example.com" and v["access"] == "read" and v["local"] is False'
t "the hook checked the key against <url>/mcp" has_line "crm.example.com/mcp" "$STUB_LOG"

# --- connect refused: curl 401 ---------------------------------------------------
CURL_CODE=401
addon_in '{"url":"https://crm.example.com","access":"read","local":false,"key_ro":"fake-refused-key"}' connect twenty --instance refused --json
expect_rc "a refused connect exits 4" 4
t "a refused connect ends with login_refused" json_ok error 'last.get("error") == "login_refused"'
no_keys "refused connect"
t "a refused connect writes nothing" absent "${store}/instances/refused.json" "${store}/secrets/refused.key_ro"
CURL_CODE=200

# --- enable: init runs crm.sh init --json, which migrates legacy.env ----------
addon enable twenty --json < /dev/null
expect_rc "enable exits 0" 0
t "enable ends with done after the init step" \
  json_ok "done" 'any(l.get("step") == "init" and l.get("status") == "done" for l in lines)'
no_keys "enable"

# --- remove while enabled: refused ---------------------------------------------
addon remove twenty --delete-data --json < /dev/null
expect_rc "remove while enabled exits 2" 2
t "remove while enabled is bad_input" json_ok error 'last.get("error") == "bad_input"'
t "a refused remove keeps the instance store" test -d "$store"
# addon.sh relays a hook's done account and mcp, not its message, and its step
# logs are temporary, so the message is checked on crm.sh itself.
out="${tmp}/out/init-direct"
err="${out}.err"
rc=0
"$BASH_UNDER_TEST" "${root}/scripts/crm.sh" init --json < /dev/null > "$out" 2> "$err" || rc=$?
expect_rc "crm.sh init --json exits 0" 0
t "crm.sh init --json ends with done: local instance off" \
  json_ok "done" 'last.get("message") == "local instance off"'
no_keys "crm.sh init --json"
t "state.json records init" json_file "${store}/state.json" '"init" in v'
t "enable sets TWENTY_MCP_ENABLED=1" has_line '^TWENTY_MCP_ENABLED=1$' "${root}/.env"
t "enable starts no service with TWENTY_LOCAL_ENABLED=0" lacks_line "compose up" "$STUB_LOG"
t "init migrated legacy.env into the store" \
  present "${store}/instances/legacy.json" "${store}/secrets/legacy.key_ro" "${store}/secrets/legacy.key_rw"
t "legacy.env is renamed .env.migrated" present "${ws}/legacy.env.migrated"
t "legacy.env is gone" absent "${ws}/legacy.env"
t "the legacy instance keeps its url and access" \
  json_file "${store}/instances/legacy.json" 'v["url"] == "https://legacy.example.com" and v["access"] == "read" and v["local"] is False'

# --- connect with a value crm.sh's hook refuses, and one addon.sh refuses ----------
addon_in '{"url":"https://u:pw@crm.example.com","access":"read","local":false,"key_ro":"fake-refused-key"}' connect twenty --instance badvalue --json
expect_rc "connect with credentials in the url exits 4" 4
t "connect with credentials in the url relays bad_value" json_ok error 'last.get("error") == "bad_value"'
no_keys "connect with credentials in the url"
addon_in '{"url":"https://crm.example.com","access":"read","local":true,"key_ro":"fake-refused-key"}' connect twenty --instance mismatch --json
expect_rc "connect local true with a remote url exits 4" 4
t "connect local true with a remote url relays local_mismatch" json_ok error 'last.get("error") == "local_mismatch"'
no_keys "connect local mismatch"
addon_in '{"url":"https://crm.example.com","access":"admin","local":false,"key_ro":"fake-refused-key"}' connect twenty --instance schema --json
expect_rc "connect with access admin exits 2" 2
t "connect with access admin is bad_input from addon.sh's schema check" \
  json_ok error 'last.get("error") == "bad_input"'
no_keys "connect with access admin"
t "refused connects write nothing" \
  absent "${store}/instances/badvalue.json" "${store}/instances/mismatch.json" "${store}/instances/schema.json"

# --- disconnect sales ------------------------------------------------------------
addon disconnect twenty --instance sales --json < /dev/null
expect_rc "disconnect exits 0" 0
t "disconnect ends with done" json_ok "done"
no_keys "disconnect"
t "disconnect removes the instance and its key" absent "${store}/instances/sales.json" "${store}/secrets/sales.key_ro"
t "disconnect leaves the other instance" present "${store}/instances/legacy.json"

# --- disable, then remove --delete-data ---------------------------------------------
# The local instance's data dir, which remove deletes with the store.
mkdir -p "${data}/twenty/db"
: > "${data}/twenty/db/PG_VERSION"
addon disable twenty --json < /dev/null
expect_rc "disable exits 0" 0
t "disable ends with done" json_ok "done"
no_keys "disable"
t "disable sets TWENTY_MCP_ENABLED=0" has_line '^TWENTY_MCP_ENABLED=0$' "${root}/.env"
t "disable keeps the instance store" test -d "$store"

addon remove twenty --delete-data --json < /dev/null
expect_rc "remove --delete-data exits 0" 0
t "remove --delete-data ends with done" json_ok "done"
no_keys "remove --delete-data"
t "remove --delete-data deletes data/addons/twenty" absent "$store"
t "remove --delete-data deletes data/twenty" absent "${data}/twenty"

# --- nothing outside the scratch root ----------------------------------------------
for k in $KEYS; do
  if grep -qF -- "$k" "$STUB_LOG"; then bad "$k in a stub's argv"; fi
done
ok "no key in any stub argv"
t "no temp file left in TMPDIR" test -z "$(ls -A "${tmp}/hosttmp")"
t "the repo's working tree is unchanged" test "$(git -C "$REPO" status --porcelain 2>/dev/null || true)" = "$repo_before"

if [ "$fail" = 0 ]; then
  echo "all Twenty add-on CLI checks passed"
else
  echo "Twenty add-on CLI checks FAILED"
  exit 1
fi
