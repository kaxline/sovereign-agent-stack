#!/bin/sh
# scripts/addon.sh against the real addons/twenty manifest, in a scratch ROOT
# with its data root outside it, host python3, and stub docker, make and curl.
# enable succeeds with no workspace stored (the instances are optional) and
# runs crm.sh init (local instance off) with one remote workspace already
# stored, and its done line carries the init hook's mcp names and message;
# enable generates the Twenty secrets .env lacks, skips one whose _FILE form
# is set and keeps a generated one on the next enable;
# connect follows; connect --force replaces an instance; crm.sh add, access
# and remove run through addon.sh;
# connect and disconnect run crm.sh's hooks (a bad value or a local mismatch
# comes back as crm.sh's error, a schema violation as addon.sh's bad_input);
# disconnect of a local instance carries the hook's "still exists in Twenty"
# message; the add-on's local setting owns TWENTY_LOCAL_ENABLED (disable then
# enable keeps it, configure {"local": false} then enable stops the local
# services and keeps hosted workspaces); remove --delete-data deletes the instance store and the local data dir, and
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
mkdir -p "${root}/scripts/lib" "$data" "${tmp}/bin" "${tmp}/out"
# addon.sh's reload step calls vault.sh reload-plugins.
printf '#!/bin/sh\nexit 0\n' > "${root}/scripts/vault.sh"
chmod +x "${root}/scripts/vault.sh"
for f in addon.sh sync-profiles.sh crm.sh twenty_admin.py; do
  cp "${REPO}/scripts/${f}" "${root}/scripts/"
done
for f in env.sh data-root.sh secret.sh python.sh mcp-probe.sh addon-human.sh addon-events.sh stack-lock.sh addon_cli.py addon_manifest.py addon_store.py; do
  cp "${REPO}/scripts/lib/${f}" "${root}/scripts/lib/"
done
cp -R "${REPO}/addons" "${root}/addons"
cp "${REPO}/docker-compose.yml" "${root}/"
# The env readers take a key's first line, so ASSISTANT_DATA_ROOT and
# TWENTY_DATA_DIR replace the example's ./data rather than being appended.
# TWENTY_LOCAL_ENABLED is 0 in the example.
grep -v -e '^ASSISTANT_DATA_ROOT=' -e '^TWENTY_DATA_DIR=' "${REPO}/.env.example" > "${root}/.env"
{
  printf 'COMPOSE_PROJECT_NAME=twentytest\n'
  printf 'ASSISTANT_DATA_ROOT=%s\n' "$data"
  # The manifest's default ./data/twenty is relative to the repo root, so the
  # local data dir is pointed into the scratch data root.
  printf 'TWENTY_DATA_DIR=%s\n' "${data}/twenty"
} >> "${root}/.env"

# A remote workspace already in the store, as addon_store writes one.
mkdir -p "${data}/addons/twenty/instances" "${data}/addons/twenty/secrets"
printf '{"url": "https://legacy.example.com", "access": "read", "local": false}\n' \
  > "${data}/addons/twenty/instances/legacy.json"
printf 'fake-legacy-ro-key' > "${data}/addons/twenty/secrets/legacy.key_ro"
printf 'fake-legacy-rw-key' > "${data}/addons/twenty/secrets/legacy.key_rw"
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
mode_of() { stat -c %a "$1" 2> /dev/null || stat -f %Lp "$1"; }
has_line() { grep -q "$1" "$2"; }
lacks_line() { ! grep -q "$1" "$2"; }
absent() { for p in "$@"; do [ ! -e "$p" ] || return 1; done; }
present() { for p in "$@"; do [ -f "$p" ] || return 1; done; }
json_file() { python3 -c "import json, sys; v = json.load(open(sys.argv[1])); sys.exit(0 if eval(sys.argv[2]) else 1)" "$1" "$2" 2> /dev/null; }

# --- enable first, with no workspace stored ----------------------------------------
# Twenty's instances are optional, so enable succeeds. Its init hook runs
# crm.sh init --json, which reports the stored legacy instance.
# The example .env has neither Twenty secret (a stack set up before v0.3.10):
# enable generates the encryption key, and skips the password, whose _FILE
# form is set (CR 0043).
printf 'TWENTY_DB_PASSWORD_FILE=/run/secrets/twenty-db\n' >> "${root}/.env"
addon enable twenty --json < /dev/null
expect_rc "enable with no workspace stored exits 0" 0
t "enable ends with done after the init step" \
  json_ok "done" 'any(l.get("step") == "init" and l.get("status") == "done" for l in lines)'
no_keys "enable"
# The init hook's done line carries the mcp names crm.sh init reports (the
# stored instance) and its message; addon.sh copies both into enable's.
t "enable's done lists crm-legacy, the name crm.sh init reports" \
  json_ok "done" 'last.get("mcp") == ["crm-legacy"]'
t "enable's done carries the init hook's message" \
  json_ok "done" 'last.get("message") == "local instance off"'
t "enable sets TWENTY_MCP_ENABLED=1" has_line '^TWENTY_MCP_ENABLED=1$' "${root}/.env"
t "enable starts no service with TWENTY_LOCAL_ENABLED=0" lacks_line "compose up" "$STUB_LOG"
t "enable generates TWENTY_ENCRYPTION_KEY, 32 bytes in base64" \
  has_line '^TWENTY_ENCRYPTION_KEY=[A-Za-z0-9+/]\{43\}=$' "${root}/.env"
t "enable skips TWENTY_DB_PASSWORD while its _FILE form is set" \
  has_line '^TWENTY_DB_PASSWORD=$' "${root}/.env"
gen_key="$(grep '^TWENTY_ENCRYPTION_KEY=' "${root}/.env" | cut -d= -f2-)"
t "enable doesn't print the generated key" lacks_line "$gen_key" "$out"

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

# --- connect --force on an existing slug: the hook takes --replace ----------------
addon_in '{"url":"https://crm.example.com","access":"read","local":false,"key_ro":"fake-ro-key"}' connect twenty --instance sales --force --json
expect_rc "connect --force on an existing slug exits 0" 0
t "connect --force ends with done and replaced" json_ok "done" 'last.get("replaced") is True and last.get("mcp") == ["crm-sales"]'
no_keys "connect --force"

# --- crm.sh add, access and remove go through addon.sh ------------------------------
# crm SCRIPT-ARGS...: crm.sh in the scratch stack, stdout in $out, stderr in $err.
crm() {
  n=$((n + 1))
  out="${tmp}/out/${n}"
  err="${tmp}/out/${n}.err"
  rc=0
  "$BASH_UNDER_TEST" "${root}/scripts/crm.sh" "$@" < /dev/null > "$out" 2> "$err" || rc=$?
  # In sh, VAR=value before a function call outlives the call.
  unset TWENTY_ACCESS TWENTY_KEY_RO TWENTY_KEY_RW
  CURL_CODE=200
}
TWENTY_ACCESS=read TWENTY_KEY_RO=fake-ro-key crm add --slug viacrm --url https://crm2.example.com/
expect_rc "crm.sh add exits 0" 0
t "crm.sh add prints a [crm] line naming crm-viacrm" has_line '^\[crm\] Connected viacrm; Hermes entries: crm-viacrm$' "$out"
no_keys "crm.sh add"
t "crm.sh add stores the url without the slash, read access and the workspace url" \
  json_file "${store}/instances/viacrm.json" 'v["url"] == "https://crm2.example.com" and v["access"] == "read" and v["workspace_url"] == "https://crm2.example.com/" and v["local"] is False'
t "crm.sh add stores the key" test "$(cat "${store}/secrets/viacrm.key_ro")" = fake-ro-key
TWENTY_ACCESS=read TWENTY_KEY_RO=fake-ro-key crm add --slug viacrm --url https://crm2.example.com
expect_rc "crm.sh add on an existing slug exits 1" 1
t "crm.sh add on an existing slug names FORCE=1" has_line 'FORCE=1' "$err"
TWENTY_KEY_RW=fake-ro-key crm add --slug viacrm --url https://crm2.example.com --force
expect_rc "crm.sh add --force exits 0" 0
t "crm.sh add --force stores access write and drops the read-only key" \
  json_file "${store}/instances/viacrm.json" 'v["access"] == "write"'
t "crm.sh add --force drops the read-only key it wasn't given" absent "${store}/secrets/viacrm.key_ro"
CURL_CODE=401 TWENTY_KEY_RW=fake-refused-key crm add --slug nope --url https://crm2.example.com
expect_rc "crm.sh add with a refused key exits 4" 4
t "crm.sh add with a refused key says so on a [crm] line" has_line '^\[crm\] ERROR: .*(login_refused)$' "$err"
t "crm.sh add with a refused key writes nothing" absent "${store}/instances/nope.json"
no_keys "crm.sh add refused"
crm access --slug viacrm read
expect_rc "crm.sh access read without a read-only key exits 1" 1
: > "$STUB_LOG"
crm access --slug viacrm write
expect_rc "crm.sh access write exits 0" 0
t "crm.sh access reloads Hermes" has_line 'compose run --rm -e HERMES_BOOTSTRAP_FORCE=1 hermes-api-bootstrap' "$STUB_LOG"
t "crm.sh access keeps its [crm] line" has_line '^\[crm\] Set TWENTY_ACCESS=write for viacrm$' "$out"
crm remove --slug viacrm
expect_rc "crm.sh remove exits 0" 0
t "crm.sh remove prints a [crm] line" has_line '^\[crm\] Disconnected viacrm$' "$out"
t "crm.sh remove deletes the instance and its key" absent "${store}/instances/viacrm.json" "${store}/secrets/viacrm.key_rw"
crm remove --slug viacrm
expect_rc "crm.sh remove of an unknown slug exits 2" 2

# --- connect refused: curl 401 ---------------------------------------------------
CURL_CODE=401
addon_in '{"url":"https://crm.example.com","access":"read","local":false,"key_ro":"fake-refused-key"}' connect twenty --instance refused --json
expect_rc "a refused connect exits 4" 4
t "a refused connect ends with login_refused" json_ok error 'last.get("error") == "login_refused"'
no_keys "refused connect"
t "a refused connect writes nothing" absent "${store}/instances/refused.json" "${store}/secrets/refused.key_ro"
CURL_CODE=200

# --- remove while enabled: refused ---------------------------------------------
addon remove twenty --delete-data --json < /dev/null
expect_rc "remove while enabled exits 2" 2
t "remove while enabled is bad_input" json_ok error 'last.get("error") == "bad_input"'
t "a refused remove keeps the instance store" test -d "$store"
t "state.json records init" json_file "${store}/state.json" '"init" in v'
grep -v '^TWENTY_DB_PASSWORD_FILE=' "${root}/.env" > "${tmp}/env.nofile"
cp "${tmp}/env.nofile" "${root}/.env"
addon enable twenty --reinit --json < /dev/null
expect_rc "enable --reinit exits 0" 0
t "a later enable keeps the generated TWENTY_ENCRYPTION_KEY" \
  has_line "^TWENTY_ENCRYPTION_KEY=${gen_key}\$" "${root}/.env"
t "a later enable generates TWENTY_DB_PASSWORD once its _FILE form is gone, in hex" \
  has_line '^TWENTY_DB_PASSWORD=[0-9a-f]\{64\}$' "${root}/.env"
t "enable --reinit passes the init message and mcp through" \
  json_ok "done" 'last.get("message") == "local instance off" and last.get("mcp") == ["crm-legacy", "crm-sales"]'
no_keys "enable --reinit"

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

# --- disconnect of a local instance: addon.sh passes the hook's message ------------
sed 's/^TWENTY_LOCAL_ENABLED=.*/TWENTY_LOCAL_ENABLED=1/' "${root}/.env" > "${tmp}/env.local"
cp "${root}/.env" "${tmp}/env.saved"
cp "${tmp}/env.local" "${root}/.env"
addon_in '{"url":"http://twenty-server:3000","access":"read","local":true,"key_ro":"fake-ro-key"}' connect twenty --instance home --json
expect_rc "connect of the local instance exits 0" 0
cp "${tmp}/env.saved" "${root}/.env"
addon disconnect twenty --instance home --json < /dev/null
expect_rc "disconnect of the local instance exits 0" 0
t "disconnect of a local instance says it still exists in Twenty" \
  json_ok "done" '"still exist" in last.get("message", "")'

# --- the local setting owns TWENTY_LOCAL_ENABLED -------------------------------------
# The local instance on, as setup.sh --crm leaves it, with no setting stored.
# plan-disable stores local=true before zeroing the gate, so plan-enable turns
# it back on (disable then enable used to lose it).
sed 's/^TWENTY_LOCAL_ENABLED=.*/TWENTY_LOCAL_ENABLED=1/' "${root}/.env" > "${tmp}/env.local"
cp "${tmp}/env.local" "${root}/.env"
(cd "$root" && "$BASH_UNDER_TEST" scripts/sync-profiles.sh > /dev/null)
t "sync-profiles adds crm with the local instance on" has_line '^COMPOSE_PROFILES=.*,crm\(,\|$\)' "${root}/.env"
settings="${store}/config/settings.env"
t "no local setting stored yet" lacks_line '^ADDON_TWENTY_LOCAL=' "$settings" 2> /dev/null
cli_plan() {
  python3 "${root}/scripts/lib/addon_cli.py" --addons-dir "${root}/addons" --data-root "$data" "$@" > "${tmp}/plan.json"
}
cli_plan plan-disable twenty --flags '{"TWENTY_MCP_ENABLED":"1","TWENTY_LOCAL_ENABLED":"1"}'
t "plan-disable zeroes TWENTY_LOCAL_ENABLED" json_file "${tmp}/plan.json" 'v["flags"]["TWENTY_LOCAL_ENABLED"] == "0"'
t "plan-disable stores local=true" has_line '^ADDON_TWENTY_LOCAL=1$' "$settings"
cli_plan plan-enable twenty
t "plan-enable after it sets TWENTY_LOCAL_ENABLED=1" json_file "${tmp}/plan.json" 'v["flags"].get("TWENTY_LOCAL_ENABLED") == "1"'
t "plan-enable lists the four local services under stop" \
  json_file "${tmp}/plan.json" 'sorted(v["stop"]) == sorted(f"twenty-{s}@TWENTY_LOCAL_ENABLED" for s in ("server", "worker", "db", "redis"))'

# configure {"local": false} on the enabled add-on stores the setting and
# writes the flag; enable then resyncs COMPOSE_PROFILES, stops the local
# services and keeps the hosted workspace registered.
addon_in '{"local":false}' configure twenty
expect_rc "configure local false exits 0" 0
t "configure stores local=false" has_line '^ADDON_TWENTY_LOCAL=0$' "$settings"
t "configure sets TWENTY_LOCAL_ENABLED=0" has_line '^TWENTY_LOCAL_ENABLED=0$' "${root}/.env"
: > "$STUB_LOG"
# --reinit runs crm.sh init again, whose done line names the registered workspaces.
addon enable twenty --reinit --json < /dev/null
expect_rc "enable after local false exits 0" 0
t "enable stops twenty-server, twenty-worker, twenty-db and twenty-redis" \
  has_line 'compose stop twenty-server twenty-worker twenty-db twenty-redis$' "$STUB_LOG"
t "enable starts no local service" lacks_line "compose up.*twenty-" "$STUB_LOG"
t "TWENTY_LOCAL_ENABLED stays 0" has_line '^TWENTY_LOCAL_ENABLED=0$' "${root}/.env"
t "enable drops crm from COMPOSE_PROFILES" lacks_line '^COMPOSE_PROFILES=.*,crm\(,\|$\)' "${root}/.env"
t "the hosted workspace stays registered" json_ok "done" '"crm-legacy" in last.get("mcp", [])'
t "the hosted workspace's store stays" present "${store}/instances/legacy.json"
no_keys "enable after local false"
# Back to the earlier state for the rest.
cp "${tmp}/env.saved" "${root}/.env"

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
