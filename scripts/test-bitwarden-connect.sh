#!/usr/bin/env bash
# Bitwarden accounts as addon.sh instances: the real addon.sh connect and
# disconnect running the real vault.sh add and remove --json as their hooks,
# in a scratch copy of the repo with host python3 and a docker stub that
# answers the broker and the reload step. Then vault.sh and the instance
# store: add writes instances/<slug>.json before the broker restart, remove
# deletes it, neither calls addon.sh, and account files from an earlier
# version are not read.
#
#   bash scripts/test-bitwarden-connect.sh
# The checks are strings check() evals after each run, and rc is read there.
# shellcheck disable=SC2016,SC2034
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BASH_UNDER_TEST=bash
[ -x /bin/bash ] && BASH_UNDER_TEST=/bin/bash

# The physical path: addon.sh resolves the data root through symlinks.
tmp="$(cd "$(mktemp -d)" && pwd -P)"
trap 'chmod -R u+rwx "$tmp" 2>/dev/null; rm -rf "$tmp"' EXIT
root="${tmp}/stack"
mkdir -p "${root}/scripts/lib" "${tmp}/bin" "${tmp}/stub" "${tmp}/hosttmp"
cp "${REPO}/scripts/addon.sh" "${REPO}/scripts/vault.sh" "${root}/scripts/"
for f in env.sh data-root.sh python.sh addon-events.sh addon_cli.py addon_manifest.py addon_store.py; do
  cp "${REPO}/scripts/lib/${f}" "${root}/scripts/lib/"
done
cp -R "${REPO}/addons" "${root}/addons"
cp "${REPO}/docker-compose.yml" "${root}/"
printf 'COMPOSE_PROJECT_NAME=bwconnect\nBITWARDEN_ENABLED=1\n' > "${root}/.env"

# docker: one log line per call. The broker is running; `broker.cli accounts`
# prints $STUB/accounts; the broker's sh -c removes bw/<slug>/ on the host;
# the bootstraps and everything else exit 0. With $STUB/fail-recreate the
# broker recreate fails.
cat > "${tmp}/bin/docker" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$STUB/log"
case " $* " in
  *" ps "*bitwarden-broker*) echo cid-broker; exit 0 ;;
  *" --force-recreate bitwarden-broker"*)
    # What the broker would load: the instance files at the restart.
    printf 'instances at recreate: %s\n' "$(ls "$STUB_DATA/instances" 2>/dev/null | tr '\n' ' ')" >> "$STUB/log"
    [ -f "$STUB/fail-recreate" ] && exit 1; exit 0 ;;
  *" broker.cli accounts "*) cat "$STUB/accounts" 2>/dev/null; exit 0 ;;
  *" exec -T bitwarden-broker sh -c "*)
    for a do last="$a"; done
    rm -rf "$STUB_DATA/bw/${last#/data/bw/}"; exit 0 ;;
esac
exit 0
SH
chmod +x "${tmp}/bin/docker"
# addon.sh stub for the vault.sh cases, put in place of the copy's addon.sh:
# logs its argv, so a call from vault.sh shows.
cat > "${tmp}/bin/addon-stub" <<'SH'
#!/bin/sh
printf 'addon.sh %s\n' "$*" >> "$STUB/addon-log"
exit 0
SH
chmod +x "${tmp}/bin/addon-stub"

export PATH="${tmp}/bin:${PATH}"
export TMPDIR="${tmp}/hosttmp"
export STACK_PYTHON=host
export STUB="${tmp}/stub"
export STUB_DATA="${root}/data/addons/bitwarden"
export VAULT_BROKER_WAIT=0
unset ASSISTANT_DATA_ROOT ENV_FILE BITWARDEN_HOST_DATA_DIR VAULT_HERMES_DATA_DIR \
  ADDONS_DIR COMPOSE_PROJECT_NAME 2>/dev/null || true

D="${root}/data/addons/bitwarden"
ACC="${root}/compose/bitwarden/accounts"
INST="${D}/instances"
PW="syn-master-$$-Zq9"
SECRET="syn-secret-$$-Kx4"
CID="user.syn-$$"

failed=0
ok() { echo "ok: $1"; }
bad() { echo "FAIL: $1"; failed=1; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; [ -f "$out" ] && sed 's/^/  | /' "$out" "$err" >&2; fi; }
mode_of() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }
n=0
out=""
err=""
rc=0
# addon ARGS... < stdin: the real addon.sh; stdout in $out, stderr in $err.
addon() {
  n=$((n + 1)); out="${tmp}/out.${n}"; err="${tmp}/err.${n}"; rc=0
  "$BASH_UNDER_TEST" "${root}/scripts/addon.sh" "$@" > "$out" 2> "$err" || rc=$?
}
connect() { printf '%s' "$2" > "${tmp}/stdin"; addon connect bitwarden --instance "$1" --json < "${tmp}/stdin"; }
# vault ARGS... < stdin: the copy's vault.sh.
vault() {
  n=$((n + 1)); out="${tmp}/out.${n}"; err="${tmp}/err.${n}"; rc=0
  "$BASH_UNDER_TEST" "${root}/scripts/vault.sh" "$@" > "$out" 2> "$err" || rc=$?
}
last() { tail -1 "$out" | python3 -c "import json,sys; v=json.load(sys.stdin).get('$1'); print('null' if v is None else v)"; }
no_secret() { ! grep -qF -e "$PW" -e "$SECRET" -e "$CID" "$out" "$err" "$STUB/log" 2>/dev/null; }

STORED="{\"mode\":\"cloud\",\"region\":\"eu\",\"unlock\":\"stored\",\"client_id\":\"$CID\",\"client_secret\":\"$SECRET\",\"master_password\":\"$PW\"}"
MANUAL="{\"mode\":\"cloud\",\"client_id\":\"$CID\",\"client_secret\":\"$SECRET\"}"

# --- connect and disconnect through addon.sh -----------------------------------
printf 'home\tcloud\tfolder\tunlocked\t-\t1\t\n' > "$STUB/accounts"
connect home "$STORED"
check "connect home exits 0" '[ "$rc" = 0 ]'
check "connect ends with done" '[ "$(last phase)" = done ]'
check "the done line names the account home" 'grep -q "\"account\":\"home\"" "$out"'
check "the store has instances/home.json" '[ -f "$INST/home.json" ]'
check "the broker restart saw instances/home.json" 'grep -q "instances at recreate: .*home.json" "$STUB/log"'
check "no account file is written" '[ ! -e "$ACC/home.env" ]'
check "home.json holds no client_id, client_secret or master_password" \
  '! grep -q -e client_id -e client_secret -e master_password -e "$CID" "$INST/home.json"'
check "home.json holds the values given, with the default scope" \
  'python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d==dict(mode=\"cloud\",region=\"eu\",unlock=\"stored\",scope=\"folder:Agent\") else 1)" "$INST/home.json"'
check "no secrets/home.* file in the store" '! ls "$D/secrets/" | grep -q "^home\."'
check "vault.sh kept the master password in secrets/home/" '[ "$(cat "$D/secrets/home/master_password")" = "$PW" ]'
check "vault.sh kept the client_id in secrets/home/" '[ "$(cat "$D/secrets/home/client_id")" = "$CID" ]'
check "secrets/ stays mode 711" '[ "$(mode_of "$D/secrets")" = 711 ]'
check "connect leaks no secret" 'no_secret'
check "connect reloads both bootstraps" 'grep -q hermes-api-bootstrap "$STUB/log" && grep -q hermes-browser-bootstrap "$STUB/log"'

connect home "$STORED"
check "a second connect of home is exists" '[ "$rc" = 2 ] && [ "$(last error)" = exists ]'

: > "$STUB/log"
printf 'bad\tcloud\tfolder\terror\tauth\t0\t\n' > "$STUB/accounts"
connect bad "$MANUAL"
check "a refused key exits 4" '[ "$rc" = 4 ]'
check "a refused key is login_refused" '[ "$(last error)" = login_refused ]'
check "a refused key leaves no store entry" '[ ! -e "$INST/bad.json" ]'
check "the refused key's instance file was there for the restart" 'grep -q "instances at recreate: .*bad.json" "$STUB/log"'
check "a refused key leaves no secrets" '[ ! -e "$D/secrets/bad" ]'
check "a refused key leaks no secret" 'no_secret'

printf 'slow\tcloud\tfolder\terror\trate_limited\t0\t90\n' > "$STUB/accounts"
connect slow "$MANUAL"
check "a rate-limited connect is rate_limited" '[ "$rc" = 4 ] && [ "$(last error)" = rate_limited ]'
check "the rate_limited line has retryAfter 90" '[ "$(last retryAfter)" = 90 ]'
check "a rate-limited connect leaves no store entry" '[ ! -e "$INST/slow.json" ]'

connect nokey '{"mode":"cloud","client_secret":"x"}'
check "connect without client_id is bad_input before the hook" '[ "$rc" = 2 ] && [ "$(last error)" = bad_input ] && [ ! -e "$INST/nokey.json" ]'

# A combination vault.sh add refuses reaches Boundary as bad_input, not command_failed.
connect allscope "{\"mode\":\"cloud\",\"scope\":\"all\",\"client_id\":\"$CID\",\"client_secret\":\"$SECRET\"}"
check "connect with scope all is bad_input from the hook, exit 2" \
  '[ "$rc" = 2 ] && [ "$(last error)" = bad_input ] && [ ! -e "$INST/allscope.json" ]'
check "the scope all refusal leaks no secret" 'no_secret'

printf 'home\tcloud\tfolder\tunlocked\t-\t1\t\n' > "$STUB/accounts"
addon disconnect bitwarden --instance home --json < /dev/null
check "disconnect home exits 0 with done" '[ "$rc" = 0 ] && [ "$(last phase)" = done ]'
check "disconnect removes the secrets" '[ ! -e "$D/secrets/home" ]'
check "disconnect removes the store entry" '[ ! -e "$INST/home.json" ]'
check "the broker restart no longer saw home.json" '! grep "instances at recreate" "$STUB/log" | tail -1 | grep -q home.json'
check "secrets/ is still mode 711" '[ "$(mode_of "$D/secrets")" = 711 ]'
addon disconnect bitwarden --instance home --json < /dev/null
check "a second disconnect is not_found" '[ "$rc" = 2 ] && [ "$(last error)" = not_found ]'

# A disconnect whose broker restart fails: vault.sh already deleted the
# instance file, and addon.sh disconnect ends in command_failed. The retry
# finds the store entry gone and ends with not_found.
printf 'retry\tcloud\tfolder\tlocked\t-\t1\t\n' > "$STUB/accounts"
connect retry "$MANUAL"
check "connect retry exits 0" '[ "$rc" = 0 ] && [ -f "$INST/retry.json" ]'
touch "$STUB/fail-recreate"
addon disconnect bitwarden --instance retry --json < /dev/null
rm -f "$STUB/fail-recreate"
check "a disconnect whose restart fails is command_failed" '[ "$rc" = 4 ] && [ "$(last error)" = command_failed ]'
check "the failed disconnect removed the instance file and secrets before the restart" \
  '[ ! -e "$INST/retry.json" ] && [ ! -e "$D/secrets/retry" ]'
addon disconnect bitwarden --instance retry --json < /dev/null
check "the retried disconnect is not_found" '[ "$rc" = 2 ] && [ "$(last error)" = not_found ]'

# --- vault.sh: terminal add and remove write the instance store ----------------
: > "$STUB/log"
printf 'term\tcloud\tfolder\tunlocked\t-\t1\t\n' > "$STUB/accounts"
printf '%s\n%s\n' "$CID" "$SECRET" > "${tmp}/stdin"
vault add --slug term --mode selfhosted --url https://vault.example.com/ --unlock manual < "${tmp}/stdin"
check "terminal add exits 0" '[ "$rc" = 0 ]'
check "terminal add writes instances/term.json" \
  'python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d==dict(mode=\"selfhosted\",url=\"https://vault.example.com\",unlock=\"manual\",scope=\"folder:Agent\") else 1)" "$INST/term.json"'
check "terminal add writes the instance file before the broker restart" 'grep -q "instances at recreate: .*term.json" "$STUB/log"'
check "terminal add leaks no secret" 'no_secret && ! grep -qF -e "$SECRET" -e "$CID" "$INST/term.json"'
vault remove --slug term < /dev/null
check "terminal remove exits 0" '[ "$rc" = 0 ]'
check "terminal remove drops term from the store" '[ ! -e "$INST/term.json" ]'

# Neither terminal nor --json add and remove call addon.sh.
cp "${root}/scripts/addon.sh" "${tmp}/addon.sh.real"
cp "${tmp}/bin/addon-stub" "${root}/scripts/addon.sh"
: > "$STUB/addon-log"
printf 'stub\tcloud\tfolder\tunlocked\t-\t1\t\n' > "$STUB/accounts"
printf '%s\n%s\n' "$CID" "$SECRET" > "${tmp}/stdin"
vault add --slug stub --mode cloud --region eu --scope collection:c-1 --unlock manual < "${tmp}/stdin"
check "terminal add of stub exits 0" '[ "$rc" = 0 ]'
check "terminal add writes region eu and the collection scope" \
  'python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d==dict(mode=\"cloud\",region=\"eu\",scope=\"collection:c-1\",unlock=\"manual\") else 1)" "$INST/stub.json"'
printf '%s\n%s\n' "$CID" "$SECRET" > "${tmp}/stdin"
vault add --slug stub --mode cloud --unlock manual --force < "${tmp}/stdin"
check "add --force replaces the instance file, the old region and scope gone" \
  '[ "$rc" = 0 ] && python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d==dict(mode=\"cloud\",region=\"us\",scope=\"folder:Agent\",unlock=\"manual\") else 1)" "$INST/stub.json"'
vault remove --slug stub < /dev/null
check "terminal remove of stub exits 0" '[ "$rc" = 0 ] && [ ! -e "$INST/stub.json" ]'
printf 'js\tcloud\tfolder\tlocked\t-\t1\t\n' > "$STUB/accounts"
printf '%s' "$MANUAL" > "${tmp}/stdin"
vault add --slug js --json < "${tmp}/stdin"
check "add --json succeeds" '[ "$rc" = 0 ] && [ "$(last phase)" = done ] && [ -f "$INST/js.json" ]'
vault remove --slug js --json < /dev/null
check "remove --json succeeds" '[ "$rc" = 0 ] && [ "$(last phase)" = done ] && [ ! -e "$INST/js.json" ]'
check "add and remove never call addon.sh" '[ ! -s "$STUB/addon-log" ]'
cp "${tmp}/addon.sh.real" "${root}/scripts/addon.sh"

# --- slugs the add-on store refuses --------------------------------------------
LONG="a2345678901234567890123456789012"
for s in 1abc "$LONG"; do
  printf '%s\n%s\n' "$CID" "$SECRET" > "${tmp}/stdin"
  vault add --slug "$s" --mode cloud --unlock manual < "${tmp}/stdin"
  check "terminal add refuses the slug $s" \
    '[ "$rc" != 0 ] && grep -q "start with a lowercase letter and have at most 31" "$out" "$err" && [ ! -e "$INST/$s.json" ] && [ ! -e "$D/secrets/$s" ]'
  printf '%s' "$MANUAL" > "${tmp}/stdin"
  vault add --slug "$s" --json < "${tmp}/stdin"
  check "add --json refuses the slug $s with bad_slug" \
    '[ "$rc" = 2 ] && [ "$(last error)" = bad_slug ] && [ ! -e "$INST/$s.json" ]'
done
printf '%s\n%s\n' "$CID" "$SECRET" > "${tmp}/stdin"
vault add --slug "${LONG%?}" --mode cloud --unlock manual < "${tmp}/stdin"
check "terminal add takes a 31-character slug" '[ "$rc" = 0 ] && [ -f "$INST/${LONG%?}.json" ]'
vault remove --slug "${LONG%?}" < /dev/null
check "terminal remove of the 31-character slug" '[ "$rc" = 0 ] && [ ! -e "$INST/${LONG%?}.json" ]'

# --- account files from an earlier version are not read ----------------------------
mkdir -p "$ACC"
printf 'BW_MODE=cloud\nBW_SERVER=https://vault.bitwarden.eu\nBW_SCOPE=folder:Agent\nBW_UNLOCK=manual\nBW_CLIENTID=user.o\n' > "$ACC/old.env"
ls "$INST" > "${tmp}/inst.before"
vault up-hook < /dev/null
check "up-hook exits 0 and ignores account files" '[ "$rc" = 0 ] && ls "$INST" | cmp -s - "${tmp}/inst.before"'
printf 'old\tcloud\tfolder\tlocked\t-\t1\t\n' > "$STUB/accounts"
vault status --json < /dev/null
check "status --json does not list an account file" '[ "$rc" = 0 ] && ! grep -q "\"old\"" "$out"'
vault remove --slug old < /dev/null
check "terminal remove of an account that is only an account file is refused" '[ "$rc" != 0 ] && [ -f "$ACC/old.env" ]'

[ "$failed" = 0 ] || { echo "test-bitwarden-connect: failures above"; exit 1; }
echo "all Bitwarden connect checks passed"
