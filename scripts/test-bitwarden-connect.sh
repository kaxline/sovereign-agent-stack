#!/usr/bin/env bash
# Bitwarden accounts as addon.sh instances: the real addon.sh connect and
# disconnect running the real vault.sh add and remove --json as their hooks,
# in a scratch copy of the repo with host python3 and a docker stub that
# answers the broker and the reload step. Then vault.sh's store sync: terminal
# add and remove call addon.sh configure, --json never does, and the make up
# hook registers account files the store lacks.
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
  *" --force-recreate bitwarden-broker"*) [ -f "$STUB/fail-recreate" ] && exit 1; exit 0 ;;
  *" broker.cli accounts "*) cat "$STUB/accounts" 2>/dev/null; exit 0 ;;
  *" exec -T bitwarden-broker sh -c "*)
    for a do last="$a"; done
    rm -rf "$STUB_DATA/bw/${last#/data/bw/}"; exit 0 ;;
esac
exit 0
SH
chmod +x "${tmp}/bin/docker"
# addon.sh stub for the vault.sh cases: logs its argv and stdin; ADDON_FAIL=1
# makes it exit 1.
cat > "${tmp}/bin/addon-stub" <<'SH'
#!/bin/sh
printf 'addon.sh %s\n' "$*" >> "$STUB/addon-log"
case " $* " in *" --remove "*) ;; *) printf 'stdin %s\n' "$(cat)" >> "$STUB/addon-log" ;; esac
[ "${ADDON_FAIL:-0}" = 1 ] && exit 1
exit 0
SH
chmod +x "${tmp}/bin/addon-stub"

export PATH="${tmp}/bin:${PATH}"
export TMPDIR="${tmp}/hosttmp"
export STACK_PYTHON=host
export STUB="${tmp}/stub"
export STUB_DATA="${root}/data/addons/bitwarden"
export VAULT_BROKER_WAIT=0
unset ASSISTANT_DATA_ROOT ENV_FILE BITWARDEN_HOST_DATA_DIR VAULT_ACCOUNTS_DIR VAULT_HERMES_DATA_DIR \
  VAULT_ADDON_SH ADDONS_DIR COMPOSE_PROJECT_NAME 2>/dev/null || true

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
mode_of() { stat -f %Lp "$1" 2>/dev/null || stat -c %a "$1"; }
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
check "the account file exists" '[ -f "$ACC/home.env" ]'
check "the store has instances/home.json" '[ -f "$INST/home.json" ]'
check "home.json holds no client_id, client_secret or master_password" \
  '! grep -q -e client_id -e client_secret -e master_password "$INST/home.json"'
check "home.json keeps the mode, region and unlock it was given" \
  'python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d==dict(mode=\"cloud\",region=\"eu\",unlock=\"stored\") else 1)" "$INST/home.json"'
check "no secrets/home.* file in the store" '! ls "$D/secrets/" | grep -q "^home\."'
check "vault.sh kept the master password in secrets/home/" '[ "$(cat "$D/secrets/home/master_password")" = "$PW" ]'
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
check "a refused key leaves no account file" '[ ! -e "$ACC/bad.env" ]'
check "a refused key leaks no secret" 'no_secret'

printf 'slow\tcloud\tfolder\terror\trate_limited\t0\t90\n' > "$STUB/accounts"
connect slow "$MANUAL"
check "a rate-limited connect is rate_limited" '[ "$rc" = 4 ] && [ "$(last error)" = rate_limited ]'
check "the rate_limited line has retryAfter 90" '[ "$(last retryAfter)" = 90 ]'
check "a rate-limited connect leaves no store entry" '[ ! -e "$INST/slow.json" ]'

connect nokey '{"mode":"cloud","client_secret":"x"}'
check "connect without client_id is bad_input before the hook" '[ "$rc" = 2 ] && [ "$(last error)" = bad_input ] && [ ! -e "$ACC/nokey.env" ]'

# A combination vault.sh add refuses reaches Boundary as bad_input, not command_failed.
connect allscope "{\"mode\":\"cloud\",\"scope\":\"all\",\"client_id\":\"$CID\",\"client_secret\":\"$SECRET\"}"
check "connect with scope all is bad_input from the hook, exit 2" \
  '[ "$rc" = 2 ] && [ "$(last error)" = bad_input ] && [ ! -e "$ACC/allscope.env" ] && [ ! -e "$INST/allscope.json" ]'
check "the scope all refusal leaks no secret" 'no_secret'

printf 'home\tcloud\tfolder\tunlocked\t-\t1\t\n' > "$STUB/accounts"
addon disconnect bitwarden --instance home --json < /dev/null
check "disconnect home exits 0 with done" '[ "$rc" = 0 ] && [ "$(last phase)" = done ]'
check "disconnect removes the account file" '[ ! -e "$ACC/home.env" ] && [ ! -e "$D/secrets/home" ]'
check "disconnect removes the store entry" '[ ! -e "$INST/home.json" ]'
check "secrets/ is still mode 711" '[ "$(mode_of "$D/secrets")" = 711 ]'
addon disconnect bitwarden --instance home --json < /dev/null
check "a second disconnect is not_found" '[ "$rc" = 2 ] && [ "$(last error)" = not_found ]'

# A disconnect whose broker restart fails keeps the store entry; the retry,
# with the account file already gone, clears it.
printf 'retry\tcloud\tfolder\tlocked\t-\t1\t\n' > "$STUB/accounts"
connect retry "$MANUAL"
check "connect retry exits 0" '[ "$rc" = 0 ] && [ -f "$INST/retry.json" ]'
touch "$STUB/fail-recreate"
addon disconnect bitwarden --instance retry --json < /dev/null
rm -f "$STUB/fail-recreate"
check "a disconnect whose restart fails is command_failed" '[ "$rc" = 4 ] && [ "$(last error)" = command_failed ]'
check "the failed disconnect removed the account file and kept the store entry" \
  '[ ! -e "$ACC/retry.env" ] && [ ! -e "$D/secrets/retry" ] && [ -f "$INST/retry.json" ]'
addon disconnect bitwarden --instance retry --json < /dev/null
check "the retried disconnect ends with done" '[ "$rc" = 0 ] && [ "$(last phase)" = done ]'
check "the retried disconnect clears the store entry" '[ ! -e "$INST/retry.json" ]'

# --- vault.sh: terminal add and remove keep the store in step -------------------
printf 'term\tcloud\tfolder\tunlocked\t-\t1\t\n' > "$STUB/accounts"
printf '%s\n%s\n' "$CID" "$SECRET" > "${tmp}/stdin"
vault add --slug term --mode selfhosted --url https://vault.example.com/ --unlock manual < "${tmp}/stdin"
check "terminal add with the real addon.sh exits 0" '[ "$rc" = 0 ]'
check "terminal add registers term in the store" \
  'python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d==dict(mode=\"selfhosted\",url=\"https://vault.example.com\",unlock=\"manual\",scope=\"folder:Agent\") else 1)" "$INST/term.json"'
check "terminal add leaks no secret" 'no_secret && ! grep -qF -e "$SECRET" -e "$CID" "$INST/term.json"'
vault remove --slug term < /dev/null
check "terminal remove exits 0" '[ "$rc" = 0 ]'
check "terminal remove drops term from the store" '[ ! -e "$INST/term.json" ]'

export VAULT_ADDON_SH="${tmp}/bin/addon-stub"
: > "$STUB/addon-log"
printf 'stub\tcloud\tfolder\tunlocked\t-\t1\t\n' > "$STUB/accounts"
printf '%s\n%s\n' "$CID" "$SECRET" > "${tmp}/stdin"
vault add --slug stub --mode cloud --region eu --scope collection:c-1 --unlock manual < "${tmp}/stdin"
check "terminal add with a stub addon.sh exits 0" '[ "$rc" = 0 ]'
check "terminal add calls addon.sh configure bitwarden --instance stub" \
  'grep -qx "addon.sh configure bitwarden --instance stub" "$STUB/addon-log"'
check "configure gets the non-secret values" \
  'grep "^stdin " "$STUB/addon-log" | sed "s/^stdin //" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d==dict(mode=\"cloud\",region=\"eu\",scope=\"collection:c-1\",unlock=\"manual\") else 1)"'
check "configure gets no secret" '! grep -qF -e "$SECRET" -e "$CID" "$STUB/addon-log"'

: > "$STUB/addon-log"
printf '%s\n%s\n' "$CID" "$SECRET" > "${tmp}/stdin"
vault add --slug stub --mode cloud --unlock manual --force < "${tmp}/stdin"
check "add --force removes the old store entry before configuring" \
  '[ "$(grep "^addon.sh" "$STUB/addon-log" | tr "\n" "|")" = "addon.sh configure bitwarden --instance stub --remove|addon.sh configure bitwarden --instance stub|" ]'

: > "$STUB/addon-log"
vault remove --slug stub < /dev/null
check "terminal remove calls configure --remove" \
  '[ "$rc" = 0 ] && grep -qx "addon.sh configure bitwarden --instance stub --remove" "$STUB/addon-log"'

: > "$STUB/addon-log"
printf 'fails\tcloud\tfolder\tunlocked\t-\t1\t\n' > "$STUB/accounts"
printf '%s\n%s\n' "$CID" "$SECRET" > "${tmp}/stdin"
ADDON_FAIL=1 vault add --slug fails --mode cloud --unlock manual < "${tmp}/stdin"
check "a failing configure does not fail terminal add" '[ "$rc" = 0 ] && [ -f "$ACC/fails.env" ]'
check "a failing configure logs a warning" 'grep -q "Warning" "$out" "$err"'
ADDON_FAIL=1 vault remove --slug fails < /dev/null
check "a failing configure --remove does not fail terminal remove" '[ "$rc" = 0 ] && [ ! -e "$ACC/fails.env" ]'

: > "$STUB/addon-log"
printf 'js\tcloud\tfolder\tlocked\t-\t1\t\n' > "$STUB/accounts"
printf '%s' "$MANUAL" > "${tmp}/stdin"
vault add --slug js --json < "${tmp}/stdin"
check "add --json succeeds" '[ "$rc" = 0 ] && [ "$(last phase)" = done ]'
vault remove --slug js --json < /dev/null
check "remove --json succeeds" '[ "$rc" = 0 ] && [ "$(last phase)" = done ]'
check "add and remove --json never call addon.sh" '[ ! -s "$STUB/addon-log" ]'

# --- slugs the add-on store refuses --------------------------------------------
# addon.sh disconnect cannot remove an account whose slug the store refuses, so
# add takes only a slug that starts with a letter and has at most 31 characters.
LONG="a2345678901234567890123456789012"
: > "$STUB/addon-log"
for s in 1abc "$LONG"; do
  printf '%s\n%s\n' "$CID" "$SECRET" > "${tmp}/stdin"
  vault add --slug "$s" --mode cloud --unlock manual < "${tmp}/stdin"
  check "terminal add refuses the slug $s" \
    '[ "$rc" != 0 ] && grep -q "start with a letter and have at most 31 characters" "$out" "$err" && [ ! -e "$ACC/$s.env" ] && [ ! -e "$D/secrets/$s" ]'
  printf '%s' "$MANUAL" > "${tmp}/stdin"
  vault add --slug "$s" --json < "${tmp}/stdin"
  check "add --json refuses the slug $s with bad_slug" \
    '[ "$rc" = 2 ] && [ "$(last error)" = bad_slug ] && [ ! -e "$ACC/$s.env" ]'
done
printf '%s\n%s\n' "$CID" "$SECRET" > "${tmp}/stdin"
vault add --slug "${LONG%?}" --mode cloud --unlock manual < "${tmp}/stdin"
check "terminal add takes a 31-character slug" '[ "$rc" = 0 ] && [ -f "$ACC/${LONG%?}.env" ]'
vault remove --slug "${LONG%?}" < /dev/null
check "refused slugs never reach addon.sh" \
  '[ "$(grep "^addon.sh" "$STUB/addon-log" | tr "\n" "|")" = "addon.sh configure bitwarden --instance ${LONG%?}|addon.sh configure bitwarden --instance ${LONG%?} --remove|" ]'

# --- make up hook: account files without a store entry -------------------------
: > "$STUB/addon-log"
mkdir -p "$ACC" "$INST"
printf 'BW_MODE=cloud\nBW_SERVER=https://vault.bitwarden.eu\nBW_SCOPE=folder:Agent\nBW_UNLOCK=manual\nBW_CLIENTID=user.o\n' > "$ACC/old.env"
printf 'BW_MODE=selfhosted\nBW_SERVER=https://vw.example.com\nBW_SCOPE=collection:c-2\nBW_UNLOCK=stored\nBW_CLIENTID=user.k\n' > "$ACC/kept.env"
printf 'BW_MODE=local\nBW_SERVER=https://vaultwarden.vault.internal\nBW_SCOPE=collection:c-agent\nBW_UNLOCK=stored\nBW_CLIENTID=user.l\n' > "$ACC/local.env"
printf '{"mode": "selfhosted"}\n' > "$INST/kept.json"
printf 'old\tcloud\tfolder\tlocked\t-\t1\t\n' > "$STUB/accounts"
vault up-hook < /dev/null
check "up-hook exits 0" '[ "$rc" = 0 ]'
check "up-hook registers old" 'grep -qx "addon.sh configure bitwarden --instance old" "$STUB/addon-log"'
check "up-hook sends the values of old" \
  'grep "^stdin " "$STUB/addon-log" | sed "s/^stdin //" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d==dict(mode=\"cloud\",region=\"eu\",scope=\"folder:Agent\",unlock=\"manual\") else 1)"'
check "up-hook skips local and accounts already in the store" \
  '[ "$(grep -c "^addon.sh" "$STUB/addon-log")" = 1 ]'
: > "$STUB/addon-log"
ADDON_FAIL=1 vault up-hook < /dev/null
check "a failing configure does not fail up-hook" '[ "$rc" = 0 ] && grep -q "Warning" "$out" "$err"'

# An account file from before add checked the slug: one line saying what to do.
: > "$STUB/addon-log"
printf 'BW_MODE=cloud\nBW_SERVER=https://vault.bitwarden.com\nBW_SCOPE=folder:Agent\nBW_UNLOCK=manual\nBW_CLIENTID=user.n\n' > "$ACC/9old.env"
vault up-hook < /dev/null
check "up-hook says Boundary cannot disconnect 9old and how to fix it" \
  '[ "$rc" = 0 ] && [ "$(grep -c "Boundary cannot disconnect 9old" "$err")" = 1 ] && grep -q "make vault-remove SLUG=9old and add it again" "$err"'
check "the 9old line promises no later fix" '! grep "9old" "$out" "$err" | grep -q "make up records"'
check "up-hook does not try to record 9old" '! grep -q "instance 9old" "$STUB/addon-log"'
vault remove --slug 9old < /dev/null
check "terminal remove of 9old skips the store and warns about nothing" \
  '[ "$rc" = 0 ] && [ ! -e "$ACC/9old.env" ] && ! grep -q "instance 9old" "$STUB/addon-log" && ! grep -q Warning "$out" "$err"'
unset VAULT_ADDON_SH
vault up-hook < /dev/null
check "up-hook with the real addon.sh writes instances/old.json" \
  '[ "$rc" = 0 ] && python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d==dict(mode=\"cloud\",region=\"eu\",scope=\"folder:Agent\",unlock=\"manual\") else 1)" "$INST/old.json"'
check "up-hook leaves kept.json alone and writes no local.json" \
  '[ "$(cat "$INST/kept.json")" = "{\"mode\": \"selfhosted\"}" ] && [ ! -e "$INST/local.json" ]'

[ "$failed" = 0 ] || { echo "test-bitwarden-connect: failures above"; exit 1; }
echo "all Bitwarden connect checks passed"
