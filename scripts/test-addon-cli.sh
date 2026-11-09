#!/bin/sh
# scripts/addon.sh against the fixture add-on in scripts/fixtures/addons-cli,
# in a scratch ROOT with host python3 and stub docker and make that log their
# argv. Runs addon.sh under /bin/bash when it exists (bash 3.2 on macOS).
#
#   sh scripts/test-addon-cli.sh
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BASH_UNDER_TEST=bash
[ -x /bin/bash ] && BASH_UNDER_TEST=/bin/bash

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
root="${tmp}/stack"
mkdir -p "${root}/scripts/lib" "${tmp}/bin" "${tmp}/out"
cp "${REPO}/scripts/addon.sh" "${REPO}/scripts/sync-profiles.sh" "${root}/scripts/" 2>/dev/null || true
for f in env.sh data-root.sh python.sh addon_cli.py addon_manifest.py addon_store.py; do
  cp "${REPO}/scripts/lib/${f}" "${root}/scripts/lib/" 2>/dev/null || true
done
cp -R "${REPO}/addons" "${root}/addons"
cp "${REPO}/.env.example" "${root}/.env"
printf 'COMPOSE_PROJECT_NAME=fixtureproj\n' >> "${root}/.env"

# docker and make stubs: log argv, one call per line. DOCKER_FAIL=1 makes
# `docker compose run` exit 7.
cat > "${tmp}/bin/docker" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "${STUB_LOG}"
if [ "${DOCKER_FAIL:-0}" = 1 ] && [ "$1 $2" = "compose run" ]; then
  echo "container output before failing"
  exit 7
fi
[ "$1 $2" = "compose run" ] && echo "container says hello"
exit 0
EOF
cat > "${tmp}/bin/make" <<'EOF'
#!/bin/sh
printf 'make %s\n' "$*" >> "${STUB_LOG}"
exit 0
EOF
chmod +x "${tmp}/bin/docker" "${tmp}/bin/make"

export PATH="${tmp}/bin:${PATH}"
export STACK_PYTHON=host
export ADDONS_DIR="${REPO}/scripts/fixtures/addons-cli"
export STUB_LOG="${tmp}/stub.log"
: > "$STUB_LOG"
unset ASSISTANT_DATA_ROOT COMPOSE_PROJECT_NAME 2>/dev/null || true

fail=0
n=0
ok() { echo "ok: $1"; }
bad() { echo "FAIL: $1"; fail=1; }

# addon [args...] < stdin: runs addon.sh, output to out/N, exit code to $rc.
addon() {
  n=$((n + 1))
  out="${tmp}/out/${n}"
  rc=0
  "$BASH_UNDER_TEST" "${root}/scripts/addon.sh" "$@" > "$out" 2>&1 || rc=$?
}
# addon_in JSON [args...]: addon with JSON on stdin. A pipe would run addon in
# a subshell and lose $rc.
addon_in() {
  printf '%s' "$1" > "${tmp}/stdin"
  shift
  addon "$@" < "${tmp}/stdin"
}
expect_rc() {
  if [ "$rc" = "$2" ]; then ok "$1"; else bad "$1: exit ${rc}, want $2"; sed 's/^/    /' "$out"; fi
}
envval() { grep -E "^$1=" "${root}/.env" | tail -1 | cut -d= -f2- || true; }
logged() { grep -qF -- "$1" "$STUB_LOG"; }

if [ ! -f "${root}/scripts/addon.sh" ]; then
  echo "FAIL: scripts/addon.sh: not found"
  exit 1
fi

addon_in '{"interval": 600}' configure fixture
expect_rc "configure top-level settings" 0
if [ -f "${root}/data/addons/fixture/config/settings.env" ] && [ ! -e "${root}/data/addons/fixture/settings.env" ]; then
  ok "settings.env is written under config/"
else
  bad "settings.env is written under config/"
fi

# usage_err ARGS...: addon.sh with stderr alone in err, exit code in $rc.
usage_err() {
  rc=0
  "$BASH_UNDER_TEST" "${root}/scripts/addon.sh" "$@" < /dev/null > /dev/null 2> "${tmp}/err" || rc=$?
}
for args in "configure fixture --bogus" "list" "run fixture" "disable fixture --nope" "enable"; do
  # shellcheck disable=SC2086
  usage_err $args
  first="$(head -n 1 "${tmp}/err")"
  case "$first" in
    "error: usage: "*) ok "usage error for '${args}' starts with error: usage:" ;;
    *) bad "usage error for '${args}' starts with error: usage:: got '${first}'" ;;
  esac
  if [ "$rc" = 2 ]; then ok "usage error for '${args}' exits 2"; else bad "usage error for '${args}' exits 2: got ${rc}"; fi
done

addon_in '{"host":"h","password":"s3cr3t-value"}' configure fixture --instance work
expect_rc "configure an instance" 0
if [ -f "${root}/data/addons/fixture/instances/work.json" ]; then ok "instance file written"; else bad "instance file written"; fi

addon_in '{"host":"h2"}' configure fixture --instance other
addon configure fixture --instance other --remove < /dev/null
expect_rc "remove an instance" 0
if [ ! -e "${root}/data/addons/fixture/instances/other.json" ]; then ok "removed instance file is gone"; else bad "removed instance file is gone"; fi

addon enable fixture
expect_rc "enable with a missing setting exits 3" 3
if grep -q token "$out"; then ok "enable names the missing setting"; else bad "enable names the missing setting"; sed 's/^/    /' "$out"; fi
if [ "$(envval FIXTURE_ADDON_ENABLED)" != 1 ]; then ok "failed enable leaves the flag off"; else bad "failed enable leaves the flag off"; fi

addon_in '{"token":"tok3n-value"}' configure fixture
expect_rc "configure the missing secret" 0

: > "$STUB_LOG"
addon enable fixture
expect_rc "enable once settings are complete" 0
if [ "$(envval FIXTURE_ADDON_ENABLED)" = 1 ]; then ok "enable sets the flag"; else bad "enable sets the flag"; fi
if logged "compose up -d --wait fixture-worker"; then ok "enable starts the service"; else bad "enable starts the service"; cat "$STUB_LOG"; fi
if logged "fixture-extra"; then bad "gated service stays off while its gate is 0"; else ok "gated service stays off while its gate is 0"; fi
if logged "make hermes-reload"; then ok "enable reloads hermes"; else bad "enable reloads hermes"; fi

addon_in '{"extra": true}' configure fixture
expect_rc "configure a gate setting" 0
if [ "$(envval FIXTURE_EXTRA_ENABLED)" = 1 ]; then ok "gate flag written to .env"; else bad "gate flag written to .env"; fi
: > "$STUB_LOG"
addon enable fixture
if logged "compose up -d --wait fixture-worker fixture-extra"; then ok "enable starts gated service when its gate is 1"; else bad "enable starts gated service when its gate is 1"; cat "$STUB_LOG"; fi

addon list --json
expect_rc "list --json" 0
if python3 -m json.tool "$out" > /dev/null; then ok "list output is JSON"; else bad "list output is JSON"; sed 's/^/    /' "$out"; fi
if python3 -c 'import json,sys; a=json.load(open(sys.argv[1])); f=[x for x in a if x["shortId"]=="fixture"][0]; sys.exit(0 if f["enabled"] and f["instances"]["work"]["password"]=={"set": True} else 1)' "$out"; then
  ok "list shows the fixture enabled with its instance"
else
  bad "list shows the fixture enabled with its instance"
fi
if python3 -c 'import json,sys; a=json.load(open(sys.argv[1])); f=[x for x in a if x["shortId"]=="fixture"][0]; m=json.load(open(sys.argv[2])); sys.exit(0 if f["schema"]=={"settings": m["settings"], "instances": m["instances"]} else 1)' "$out" "${ADDONS_DIR}/fixture/addon.json"; then
  ok "list carries the fixture's settings schema"
else
  bad "list carries the fixture's settings schema"
fi

: > "$STUB_LOG"
addon run fixture sync --instance work
expect_rc "run an instance command" 0
if logged "compose run --rm --no-deps -T fixture-worker fixture-sync --once --account work"; then ok "run passes argv and --account"; else bad "run passes argv and --account"; cat "$STUB_LOG"; fi
if grep -q "container says hello" "$out"; then ok "run prints the container's output"; else bad "run prints the container's output"; fi
addon run fixture status
if logged "compose run --rm --no-deps -T fixture-worker fixture-status two words"; then ok "run without instance appends no --account"; else bad "run without instance appends no --account"; fi
addon run fixture sync
expect_rc "instance command without --instance exits 2" 2
addon run fixture sync --instance nobody
expect_rc "unknown instance exits 2" 2
addon run fixture nope
expect_rc "unknown command exits 2" 2
DOCKER_FAIL=1 addon run fixture status
expect_rc "failing container exits 4" 4

addon enable nosuch
expect_rc "unknown short id exits 2" 2

addon_in '{' configure fixture
expect_rc "invalid JSON exits 2" 2
addon_in '{"bogus": 1}' configure fixture
expect_rc "unknown setting exits 2" 2
addon_in '{"interval": "s3cr3t-value"}' configure fixture
expect_rc "wrong type exits 2" 2
if grep -q interval "$out"; then ok "type error names the setting"; else bad "type error names the setting"; fi

: > "$STUB_LOG"
addon disable fixture
expect_rc "disable" 0
if [ "$(envval FIXTURE_ADDON_ENABLED)" = 0 ] && [ "$(envval FIXTURE_EXTRA_ENABLED)" = 0 ]; then
  ok "disable sets the flag and gate flags to 0"
else
  bad "disable sets the flag and gate flags to 0"
fi
if logged "compose stop fixture-worker fixture-extra"; then ok "disable stops every service"; else bad "disable stops every service"; cat "$STUB_LOG"; fi
if logged "make hermes-reload"; then ok "disable reloads hermes"; else bad "disable reloads hermes"; fi
if [ -f "${root}/data/addons/fixture/secrets/token" ]; then ok "disable keeps the data"; else bad "disable keeps the data"; fi
if logged "volume rm"; then bad "disable without --purge removes no volume"; else ok "disable without --purge removes no volume"; fi

# configure while disabled stores a gate setting but leaves .env alone.
addon_in '{"extra": true}' configure fixture
expect_rc "configure a gate setting while disabled" 0
if [ "$(envval FIXTURE_EXTRA_ENABLED)" = 0 ] && [ "$(envval FIXTURE_ADDON_ENABLED)" = 0 ]; then
  ok "configure while disabled leaves the gate flags in .env"
else
  bad "configure while disabled leaves the gate flags in .env: extra=$(envval FIXTURE_EXTRA_ENABLED)"
fi

# Enabling again restores the gate from the stored setting.
: > "$STUB_LOG"
addon enable fixture
if [ "$(envval FIXTURE_EXTRA_ENABLED)" = 1 ] && logged "fixture-extra"; then
  ok "enable restores a gate from its stored setting"
else
  bad "enable restores a gate from its stored setting"
fi

for f in "${root}/.env" "$STUB_LOG" "${tmp}"/out/*; do
  if grep -q -e s3cr3t-value -e tok3n-value "$f"; then bad "secret value appears in ${f#"${tmp}/"}"; fi
done
grep -r -q -e s3cr3t-value -e tok3n-value "${root}/.env" "$STUB_LOG" "${tmp}/out" || ok "no secret value in .env, stub logs or output"

# A failure inside addon_cli.py is one error line, exit 1, no traceback.
mkdir -p "${root}/data/addons/fixture/locked"
: > "${root}/data/addons/fixture/locked/file"
chmod 500 "${root}/data/addons/fixture/locked"
addon disable fixture --purge
chmod 700 "${root}/data/addons/fixture/locked"
expect_rc "a purge that cannot delete exits 1" 1
if grep -q '^error: PermissionError: ' "$out" && ! grep -q Traceback "$out"; then
  ok "an unexpected failure is one error line"
else
  bad "an unexpected failure is one error line"; sed 's/^/    /' "$out"
fi

: > "$STUB_LOG"
addon disable fixture --purge
expect_rc "disable --purge" 0
if [ ! -e "${root}/data/addons/fixture" ]; then ok "--purge removes data/addons/fixture"; else bad "--purge removes data/addons/fixture"; fi
if logged "volume rm fixtureproj_fixture_cache"; then ok "--purge removes the declared volume"; else bad "--purge removes the declared volume"; cat "$STUB_LOG"; fi
if [ -d "${root}/data/addons" ]; then ok "--purge leaves data/addons"; else bad "--purge leaves data/addons"; fi

mkdir -p "${root}/data/addons/keep"
addon disable .. --purge
expect_rc "disable .. --purge exits 2" 2
addon disable "" --purge
expect_rc "disable with an empty short id exits 2" 2
if [ -d "${root}/data/addons/keep" ] && [ -f "${root}/.env" ]; then ok "a bad short id deletes nothing"; else bad "a bad short id deletes nothing"; fi

addon frobnicate fixture
expect_rc "unknown subcommand exits 2" 2

if [ "$fail" = 0 ]; then
  echo "all addon.sh checks passed"
else
  exit 1
fi
