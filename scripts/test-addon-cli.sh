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

# The physical path: addon.sh resolves the data root through symlinks
# (/var -> /private/var on macOS), and the python3 guard below compares paths.
tmp="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$tmp"' EXIT
root="${tmp}/stack"
mkdir -p "${root}/scripts/lib" "${tmp}/bin" "${tmp}/out"
cp "${REPO}/scripts/addon.sh" "${REPO}/scripts/sync-profiles.sh" "${REPO}/scripts/fixture-init.sh" "${REPO}/scripts/fixture-connect.sh" "${REPO}/scripts/fixture-follow.sh" "${root}/scripts/" 2>/dev/null || true
for f in env.sh data-root.sh python.sh addon-events.sh addon_cli.py addon_manifest.py addon_store.py; do
  cp "${REPO}/scripts/lib/${f}" "${root}/scripts/lib/" 2>/dev/null || true
done
cp -R "${REPO}/addons" "${root}/addons"
cp "${REPO}/.env.example" "${root}/.env"
printf 'COMPOSE_PROJECT_NAME=fixtureproj\n' >> "${root}/.env"

# docker and make stubs: log argv, one call per line. DOCKER_FAIL=1 makes
# `docker compose run` exit 7. DOCKER_FAIL_UP=1 makes `compose up` print a
# quote, a backslash, a 10,000-character line and an ANSI escape, then exit 1.
# DOCKER_SLEEP_UP=1 makes `compose up` write its PID to SLEEP_PID and sleep.
# DOCKER_FAIL_PULL=1 makes `compose --progress json pull` exit 1.
# DOCKER_UP_DELAY=N makes `compose up` sleep N seconds first.
# DOCKER_FAIL_RUN=N makes `docker compose run` exit N after printing a line.
# DOCKER_FAIL_ONESHOT=1 makes `compose up --no-deps --exit-code-from` exit 1.
# `compose ps -a --format json` prints the file PS_ANSWER names, if any.
# `compose logs` prints one line naming the service.
# `compose config --services` prints the file CONFIG_SERVICES names, if any.
# DOCKER_FAIL_RECREATE=1 makes `compose up ... --force-recreate` exit 1.
# DOCKER_FAIL_BOOTSTRAP=1 makes `compose run ... hermes-api-bootstrap` exit 1.
cat > "${tmp}/bin/docker" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "${STUB_LOG}"
if [ "$1 $2 $3 $4" = "compose up --no-deps --exit-code-from" ] && [ "${DOCKER_FAIL_ONESHOT:-0}" = 1 ]; then
  echo "seed failed: no such table"
  exit 1
fi
if [ "$1 $2 $3 $4" = "compose ps -a --format" ]; then
  [ -n "${PS_ANSWER:-}" ] && cat "$PS_ANSWER"
  exit 0
fi
if [ "$1 $2 $3" = "compose config --services" ]; then
  [ -n "${CONFIG_SERVICES:-}" ] && cat "$CONFIG_SERVICES"
  exit 0
fi
if [ "${DOCKER_FAIL_BOOTSTRAP:-0}" = 1 ] && [ "$1 $2" = "compose run" ]; then
  case "$*" in *hermes-api-bootstrap*) echo "bootstrap failed"; exit 1 ;; esac
fi
if [ "$1 $2" = "compose logs" ]; then
  eval "last=\${$#}"
  echo "log line from $last"
  exit 0
fi
if [ "$1 $2" = "compose up" ] && [ "${DOCKER_FAIL_RECREATE:-0}" = 1 ]; then
  case "$*" in *--force-recreate*) echo "recreate failed"; exit 1 ;; esac
fi
if [ "$1 $2" = "compose up" ]; then
  [ -n "${DOCKER_UP_DELAY:-}" ] && sleep "$DOCKER_UP_DELAY"
  if [ "${DOCKER_SLEEP_UP:-0}" = 1 ]; then
    echo $$ > "${SLEEP_PID}"
    exec sleep 30
  fi
  if [ "${DOCKER_FAIL_UP:-0}" = 1 ] && [ "$3" = "-d" ]; then
    printf '%s\n' 'boom "quoted" \ back'
    printf '%010000d\n' 0
    printf 'colour \033[31mred\033[0m\tend\n'
    exit 1
  fi
fi
if [ "$1 $2 $3 $4" = "compose --progress json pull" ]; then
  if [ "${DOCKER_FAIL_PULL:-0}" = 1 ]; then
    echo '{"id":"Image example/x:1","status":"Error","text":"pull access denied"}' >&2
    exit 1
  fi
  echo '{"id":"Image example/x:1","status":"Working","text":"Pulling"}' >&2
  echo '{"id":"Image example/x:1","status":"Done","text":"Pulled"}' >&2
fi
if [ -n "${DOCKER_FAIL_RUN:-}" ] && [ "$1 $2" = "compose run" ]; then
  echo "init container failing"
  exit "$DOCKER_FAIL_RUN"
fi
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
# scripts/vault.sh stub: logs its argv; VAULT_FAIL=1 makes it exit 1. The real
# one lives in the repo and is never run here.
cat > "${root}/scripts/vault.sh" <<'VAULT_EOF'
#!/bin/sh
printf 'vault.sh %s\n' "$*" >> "${STUB_LOG}"
[ "${VAULT_FAIL:-0}" = 1 ] && { echo "reload-plugins failed"; exit 1; }
exit 0
VAULT_EOF
chmod +x "${root}/scripts/vault.sh"

# Guard: addon_cli.py runs in the Hermes container in production, which mounts
# only the repo, the data root and ADDONS_DIR (${tmp}/dataroot is the data root
# of the ASSISTANT_DATA_ROOT case). A python3 shim fails any
# addon_cli.py call that has an argument naming an existing path elsewhere (a
# host temp file, say). TMPDIR is outside the scratch root for the same reason.
real_python3="$(command -v python3)"
cat > "${tmp}/bin/python3" <<GUARD_EOF
#!/bin/sh
case "\$*" in
  *addon_cli.py*)
    skip=0
    for a in "\$@"; do
      if [ "\$skip" = 1 ]; then skip=0; continue; fi
      case "\$a" in
        --addons-dir | --data-root) skip=1; continue ;;
        "${root}" | "${root}"/* | "${REPO}"/* | "${tmp}/dataroot"/*) continue ;;
        /*) ;;
        *) continue ;;
      esac
      if [ -e "\$a" ]; then echo "error: host path passed to addon_cli.py: \$a" >&2; exit 99; fi
    done
    ;;
esac
exec "${real_python3}" "\$@"
GUARD_EOF
chmod +x "${tmp}/bin/python3"
mkdir -p "${tmp}/hosttmp"
export TMPDIR="${tmp}/hosttmp"
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
# addon_json [args...]: addon with stdout alone in $out and stderr in $err.
addon_json() {
  n=$((n + 1))
  out="${tmp}/out/${n}"
  err="${tmp}/out/${n}.err"
  rc=0
  "$BASH_UNDER_TEST" "${root}/scripts/addon.sh" "$@" < /dev/null > "$out" 2> "$err" || rc=$?
}
# json_ok FILE PHASE [EXPR]: every line of FILE is a JSON object, the last
# line's phase is PHASE, and the Python expression EXPR holds over `lines`
# (the parsed objects), `last` and `steps` ((step, status) of setup lines).
json_ok() {
  python3 -c 'import json, os, sys
lines = [json.loads(l) for l in open(sys.argv[1]).read().splitlines()]
assert lines and all(isinstance(l, dict) for l in lines)
last = lines[-1]
steps = [(l["step"], l["status"]) for l in lines if l["phase"] == "setup"]
assert last["phase"] == sys.argv[2]
sys.exit(0 if eval(sys.argv[3]) else 1)' "$1" "$2" "${3:-True}" 2> /dev/null
}
# check NAME EXPR-STATUS: ok or FAIL with the output file shown.
check() {
  if [ "$2" = 0 ]; then ok "$1"; else bad "$1"; sed 's/^/    /' "$out"; fi
}
expect_rc() {
  if [ "$rc" = "$2" ]; then ok "$1"; else bad "$1: exit ${rc}, want $2"; sed 's/^/    /' "$out"; fi
}
envval() { grep -E "^$1=" "${root}/.env" | tail -1 | cut -d= -f2- || true; }
logged() { grep -qF -- "$1" "$STUB_LOG"; }
# reloaded: the log holds the forced bootstraps in order, then reload-plugins,
# and no make, recreate or follow-hermes.
reloaded() {
  python3 -c 'import sys
log = open(sys.argv[1]).read().splitlines()
want = ["compose run --rm -e HERMES_BOOTSTRAP_FORCE=1 hermes-api-bootstrap",
        "compose run --rm --no-deps -e HERMES_BOOTSTRAP_FORCE=1 hermes-browser-bootstrap",
        "vault.sh reload-plugins"]
at = [log.index(w) if w in log else -1 for w in want]
sys.exit(0 if -1 not in at and at == sorted(at)
         and not any(l.startswith("make") or "--force-recreate" in l or "follow-hermes" in l for l in log) else 1)' "$STUB_LOG"
}

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
if logged "compose run --rm --no-deps -T fixture-worker fixture-seed-init --json"; then ok "the first enable runs the init hook"; else bad "the first enable runs the init hook"; cat "$STUB_LOG"; fi
if [ "$(stat -f %Lp "${root}/data/addons/fixture/state.json" 2> /dev/null || stat -c %a "${root}/data/addons/fixture/state.json")" = 600 ] \
  && python3 -c 'import json,sys; i=json.load(open(sys.argv[1]))["init"]; sys.exit(0 if i["version"]=="1.0.0" and i["at"].endswith("Z") else 1)' "${root}/data/addons/fixture/state.json"; then
  ok "enable records init in state.json"
else
  bad "enable records init in state.json"
fi
if logged "fixture-extra"; then bad "gated service stays off while its gate is 0"; else ok "gated service stays off while its gate is 0"; fi
if reloaded; then ok "enable reloads hermes with the bootstraps and reload-plugins"; else bad "enable reloads hermes with the bootstraps and reload-plugins"; cat "$STUB_LOG"; fi

addon_in '{"extra": true}' configure fixture
expect_rc "configure a gate setting" 0
if [ "$(envval FIXTURE_EXTRA_ENABLED)" = 1 ]; then ok "gate flag written to .env"; else bad "gate flag written to .env"; fi
: > "$STUB_LOG"
addon enable fixture
if logged "fixture-seed-init"; then bad "a second enable does not run init again"; else ok "a second enable does not run init again"; fi
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
unset DOCKER_FAIL
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
if reloaded; then ok "disable reloads hermes with the bootstraps and reload-plugins"; else bad "disable reloads hermes with the bootstraps and reload-plugins"; cat "$STUB_LOG"; fi
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

# --json output, the lock and rollback. The fixture is enabled here.
ENABLE_STEPS='[("flags","running"),("flags","done"),("start","running"),("start","done"),("start","running"),("start","done"),("reload","running"),("reload","done")]'
: > "$STUB_LOG"
addon_json disable fixture --json
expect_rc "disable --json" 0
r=0; json_ok "$out" "done" 'steps == [("flags","running"),("flags","done"),("stop","running"),("stop","done"),("reload","running"),("reload","done")] and last == {"phase":"done","id":"fixture","action":"disable","restart":[],"mcp":[]}' || r=1
check "disable --json prints flags, stop and reload, then done" "$r"

: > "$STUB_LOG"
addon_json enable --json fixture
expect_rc "enable --json" 0
r=0; json_ok "$out" "done" "steps == ${ENABLE_STEPS} and last == {'phase':'done','id':'fixture','action':'enable','restart':[],'mcp':[]}" || r=1
check "enable --json prints flags, start and reload, then done" "$r"
r=0; json_ok "$out" "done" '[l["service"] for l in lines if l["phase"] == "pull" and l.get("status") == "done"] == ["fixture-worker", "fixture-extra", "fixture-seed"]' || r=1
check "enable --json prints a pull done line per service it starts" "$r"
if [ "$(grep -n 'pull' "$STUB_LOG" | head -1 | cut -d: -f1)" = 1 ] && logged "compose --progress json pull --policy missing --ignore-buildable fixture-worker"; then
  ok "enable --json pulls missing images before anything else"
else
  bad "enable --json pulls missing images before anything else"; cat "$STUB_LOG"
fi

# One-shots run before the wait and are left out of it.
addon disable fixture
: > "$STUB_LOG"
addon_json enable fixture --json
n_seed="$(grep -n 'compose up --no-deps --exit-code-from fixture-seed fixture-seed' "$STUB_LOG" | head -1 | cut -d: -f1)"
n_wait="$(grep -n 'compose up -d --wait fixture-worker' "$STUB_LOG" | head -1 | cut -d: -f1)"
if [ -n "$n_seed" ] && [ -n "$n_wait" ] && [ "$n_seed" -lt "$n_wait" ] && ! grep -F 'up -d --wait' "$STUB_LOG" | grep -qF fixture-seed; then
  ok "enable runs the one-shot before the wait and leaves it out of it"
else
  bad "enable runs the one-shot before the wait and leaves it out of it"; cat "$STUB_LOG"
fi
r=0; json_ok "$out" "done" '[(l["step"], l.get("target")) for l in lines if l["phase"] == "setup" and l["step"] == "start" and l["status"] == "running"] == [("start", "fixture-seed"), ("start", None)]' || r=1
check "the one-shot start step names its service" "$r"

# A one-shot that exits non-zero stops enable before the wait.
addon disable fixture
cp "${root}/.env" "${tmp}/env.before"
: > "$STUB_LOG"
DOCKER_FAIL_ONESHOT=1 addon_json enable fixture --json
unset DOCKER_FAIL_ONESHOT
expect_rc "enable --json with a failing one-shot exits 1" 1
r=0; json_ok "$out" error 'last["error"] == "compose_failed" and last["step"] == "start" and last["target"] == "fixture-seed" and last.get("rolledBack") is True and "seed failed: no such table" in last["logTail"]' || r=1
check "a failing one-shot is compose_failed at step start with its target" "$r"
if ! logged "up -d --wait"; then ok "a failing one-shot never reaches the wait"; else bad "a failing one-shot never reaches the wait"; cat "$STUB_LOG"; fi
if cmp -s "${root}/.env" "${tmp}/env.before"; then ok "a failing one-shot rolls back .env"; else bad "a failing one-shot rolls back .env"; fi

# The wait fails; ps shows which container exited (n8n-import blamed on n8n).
cat > "${tmp}/ps-array.json" <<'PSEOF'
[{"Service":"fixture-worker","State":"created","ExitCode":0},{"Service":"fixture-seed","State":"exited","ExitCode":1}]
PSEOF
: > "$STUB_LOG"
PS_ANSWER="${tmp}/ps-array.json" DOCKER_FAIL_UP=1 addon_json enable fixture --json
unset PS_ANSWER DOCKER_FAIL_UP
expect_rc "enable --json with the wait failing exits 1" 1
r=0; json_ok "$out" error 'last["error"] == "compose_failed" and last["step"] == "start" and last["target"] == "fixture-seed" and last["logTail"] == ["log line from fixture-seed"]' || r=1
check "a failed wait reports the exited container and its logs" "$r"
if logged "compose logs --no-color --tail 20 fixture-seed" && logged "compose ps -a --format json"; then ok "a failed wait asks ps and logs"; else bad "a failed wait asks ps and logs"; cat "$STUB_LOG"; fi

# The init hook: --reinit, a failing hook, a script hook and error names.
STATE="${root}/data/addons/fixture/state.json"
addon disable fixture
: > "$STUB_LOG"
addon_json enable fixture --reinit --json
expect_rc "enable --reinit --json" 0
if logged "compose run --rm --no-deps -T fixture-worker fixture-seed-init --json"; then ok "--reinit runs init again"; else bad "--reinit runs init again"; cat "$STUB_LOG"; fi
r=0; json_ok "$out" "done" 'steps == [("flags","running"),("flags","done"),("start","running"),("start","done"),("start","running"),("start","done"),("init","running"),("init","done"),("reload","running"),("reload","done")]' || r=1
check "init is a step between start and reload" "$r"
addon_json enable fixture --json
if [ "$(grep -c fixture-seed-init "$STUB_LOG")" = 1 ]; then ok "enable without --reinit skips a recorded init"; else bad "enable without --reinit skips a recorded init"; cat "$STUB_LOG"; fi
r=0; json_ok "$out" "done" '("init","running") not in steps' || r=1
check "a skipped init prints no init step" "$r"

addon disable fixture
cp "$STATE" "${tmp}/state.before"
DOCKER_FAIL_RUN=5 addon_json enable fixture --reinit --json
unset DOCKER_FAIL_RUN
expect_rc "--reinit with a failing hook exits 4" 4
if cmp -s "$STATE" "${tmp}/state.before"; then ok "a failed --reinit keeps the recorded init"; else bad "a failed --reinit keeps the recorded init"; fi
rm -f "$STATE"
cp "${root}/.env" "${tmp}/env.before"
: > "$STUB_LOG"
DOCKER_FAIL_RUN=5 addon_json enable fixture --json
unset DOCKER_FAIL_RUN
expect_rc "a failing init hook exits 4" 4
r=0; json_ok "$out" error 'last["error"] == "command_failed" and last["step"] == "init" and last.get("rolledBack") is True and ("init","error") in steps and "init container failing" in last["logTail"]' || r=1
check "a failing init is command_failed at step init and rolls back" "$r"
if cmp -s "${root}/.env" "${tmp}/env.before" && [ ! -e "$STATE" ]; then ok "a failed init restores .env and records nothing"; else bad "a failed init restores .env and records nothing"; fi
: > "$STUB_LOG"
addon_json enable fixture --json
expect_rc "enable after a failed init" 0
if logged "fixture-seed-init" && [ -f "$STATE" ]; then ok "a failed init runs again on the next enable"; else bad "a failed init runs again on the next enable"; fi

# A corrupt state.json fails enable (command_failed) and is left alone.
addon disable fixture
printf '{broken' > "$STATE"
addon_json enable fixture --json
expect_rc "a corrupt state.json fails enable with 4" 4
r=0; json_ok "$out" error 'last["error"] == "command_failed" and last["step"] == "init" and last.get("rolledBack") is True and ("init","done") not in steps' || r=1
check "a corrupt state.json is command_failed at step init" "$r"
if [ "$(cat "$STATE")" = '{broken' ]; then ok "a corrupt state.json is not overwritten"; else bad "a corrupt state.json is not overwritten"; fi
rm -f "$STATE"
addon_json enable fixture --json

# A script hook, from a second fixture (it has one required instance).
addon_in '{"host":"h"}' configure hooked --instance main
: > "$STUB_LOG"
addon_json enable hooked --json
expect_rc "enable with a script init hook" 0
r=0; json_ok "$out" "done" 'steps == [("flags","running"),("flags","done"),("start","running"),("start","done"),("init","running"),("init","running"),("init","done"),("reload","running"),("reload","done")] and last == {"phase":"done","id":"hooked","action":"enable","restart":[],"mcp":[],"account":"main"} and [l["phase"] for l in lines].count("done") == 1' || r=1
check "a script hook's done line gives the account, once" "$r"
r=0; json_ok "$out" "done" '[i for i, l in enumerate(lines) if l.get("message") == "creating budget"] == [[i for i, l in enumerate(lines) if l.get("step") == "init" and l.get("status") == "running"][0] + 1] and lines[[i for i, l in enumerate(lines) if l.get("message") == "creating budget"][0]]["step"] == "init"' || r=1
check "the hook's progress line sits inside the init step with step set" "$r"
if logged "compose run --rm --no-deps -T"; then bad "a script hook does not use compose run"; else ok "a script hook does not use compose run"; fi
addon disable hooked
for mode in exit5 error; do
  rm -f "${root}/data/addons/hooked/state.json"
  case "$mode" in
    exit5) FIXTURE_INIT_MODE=exit5 addon_json enable hooked --json; want_err=command_failed ;;
    error) FIXTURE_INIT_MODE=error FIXTURE_INIT_ERROR=bad_slug addon_json enable hooked --json; want_err=bad_slug ;;
  esac
  expect_rc "script hook ${mode} exits 4" 4
  r=0; json_ok "$out" error "last['error'] == '${want_err}' and last['step'] == 'init' and last.get('rolledBack') is True" || r=1
  check "script hook ${mode} is ${want_err} at step init" "$r"
  if [ ! -e "${root}/data/addons/hooked/state.json" ]; then ok "script hook ${mode} records no init"; else bad "script hook ${mode} records no init"; fi
done
FIXTURE_INIT_MODE=error FIXTURE_INIT_ERROR=weird_thing addon_json enable hooked --json
r=0; json_ok "$out" error 'last["error"] == "command_failed" and "weird_thing" in last["message"] and "hook says no" in last["message"]' || r=1
check "an unlisted hook error becomes command_failed with its name in the message" "$r"
# An error name in instances.errors passes through.
FIXTURE_INIT_MODE=error FIXTURE_INIT_ERROR=encryption_password addon_json enable hooked --json
expect_rc "a hook error listed in instances.errors exits 4" 4
r=0; json_ok "$out" error 'last["error"] == "encryption_password" and last["step"] == "init" and last["message"] == "hook says no" and steps[-1] == ("init","error")' || r=1
check "a listed hook error passes through unchanged" "$r"
# An error line fails the step even when the hook exits 0, and init never shows done.
FIXTURE_INIT_MODE=error0 FIXTURE_INIT_ERROR=bad_slug addon_json enable hooked --json
expect_rc "an error line with exit 0 still fails" 4
r=0; json_ok "$out" error 'last["error"] == "bad_slug" and ("init","done") not in steps and ("init","error") in steps' || r=1
check "an error line with exit 0 shows init error, not done" "$r"
unset FIXTURE_INIT_MODE FIXTURE_INIT_ERROR 2> /dev/null || true

# connect and disconnect. hooked has connect and disconnect script hooks
# (scripts/fixture-connect.sh logs "fixture-connect MODE ARGV" to STUB_LOG);
# fixture has none.
# addon_json_in JSON [args...]: addon_json with JSON on stdin.
addon_json_in() {
  printf '%s' "$1" > "${tmp}/stdin"
  shift
  n=$((n + 1))
  out="${tmp}/out/${n}"
  err="${tmp}/out/${n}.err"
  rc=0
  "$BASH_UNDER_TEST" "${root}/scripts/addon.sh" "$@" < "${tmp}/stdin" > "$out" 2> "$err" || rc=$?
}
# no_staging NAME: no .staging.* file is left in data/addons.
no_staging() {
  if [ -n "$(find "${root}/data/addons" -maxdepth 1 -name '.staging.*')" ]; then bad "no staging file after ${1}"; else ok "no staging file after ${1}"; fi
}
HOOKED="${root}/data/addons/hooked"
: > "$STUB_LOG"
addon_json_in '{"bogus":1}' connect hooked --instance work --json
expect_rc "connect with an unknown setting exits 2" 2
r=0; json_ok "$out" error 'last["error"] == "bad_input"' || r=1
check "connect with an unknown setting is bad_input" "$r"
if logged "fixture-connect"; then bad "bad_input is found before the hook runs"; else ok "bad_input is found before the hook runs"; fi
no_staging "bad_input"

: > "$STUB_LOG"
addon_json_in '{"host":"refuse.example.com","password":"s3cr3t-value\"x"}' connect hooked --instance work --json
expect_rc "a refused connect exits 4" 4
r=0; json_ok "$out" error 'last["error"] == "login_refused" and last["step"] == "check" and last["message"] == "refused" and ("check","error") in steps and ("register","running") not in steps' || r=1
check "a refused connect is login_refused at step check" "$r"
r=0; json_ok "$out" error 'any("[redacted]" in t for t in last["logTail"]) and not any("s3cr3t" in t for t in last["logTail"]) and any("refuse.example.com" in t for t in last["logTail"])' || r=1
check "a refused connect's logTail shows [redacted] in place of the password" "$r"
if [ ! -e "${HOOKED}/instances/work.json" ] && [ -z "$(find "${HOOKED}" -path '*/secrets/work.*' 2> /dev/null)" ]; then ok "a refused connect writes no instance"; else bad "a refused connect writes no instance"; fi
no_staging "a refused connect"

: > "$STUB_LOG"
addon_json_in '{"host":"good.example.com","password":"s3cr3t-value\nline2"}' connect hooked --instance work --json
expect_rc "a multi-line secret exits 2" 2
r=0; json_ok "$out" error 'last["error"] == "bad_input" and "password" in last["message"]' || r=1
check "a multi-line secret is bad_input naming the setting" "$r"
if logged "fixture-connect"; then bad "a multi-line secret is refused before the hook runs"; else ok "a multi-line secret is refused before the hook runs"; fi
addon_in '{"host":"h","password":"s3cr3t-value\rline2"}' configure fixture --instance multi
expect_rc "configure --instance refuses a secret with CR" 2
if [ ! -e "${root}/data/addons/fixture/instances/multi.json" ]; then ok "a refused multi-line secret writes no instance"; else bad "a refused multi-line secret writes no instance"; fi

FIXTURE_CONNECT_MODE=exit1 addon_json_in '{"host":"good.example.com"}' connect hooked --instance work --json
unset FIXTURE_CONNECT_MODE
expect_rc "a connect hook exiting 1 with no error line exits 4" 4
r=0; json_ok "$out" error 'last["error"] == "command_failed" and last["step"] == "check" and "status 1" in last["message"] and ("register","running") not in steps' || r=1
check "a connect hook exiting 1 with no error line is command_failed at step check" "$r"
if [ ! -e "${HOOKED}/instances/work.json" ]; then ok "a connect hook exiting 1 writes no instance"; else bad "a connect hook exiting 1 writes no instance"; fi

FIXTURE_CONNECT_ERROR=encryption_password addon_json_in '{"host":"good.example.com"}' connect hooked --instance work --json
unset FIXTURE_CONNECT_ERROR
expect_rc "a listed connect error exits 4" 4
r=0; json_ok "$out" error 'last["error"] == "encryption_password" and last["step"] == "check"' || r=1
check "a connect error in instances.errors passes through" "$r"
FIXTURE_CONNECT_ERROR=weird addon_json_in '{"host":"good.example.com"}' connect hooked --instance work --json
unset FIXTURE_CONNECT_ERROR
expect_rc "an unlisted connect error exits 4" 4
r=0; json_ok "$out" error 'last["error"] == "command_failed" and "weird" in last["message"] and last["step"] == "check"' || r=1
check "an unlisted connect error becomes command_failed naming it" "$r"
if [ ! -e "${HOOKED}/instances/work.json" ]; then ok "a failed connect hook writes no instance"; else bad "a failed connect hook writes no instance"; fi
no_staging "a failed connect hook"

FIXTURE_CONNECT_MODE=stderr_error addon_json_in '{"host":"good.example.com"}' connect hooked --instance work --json
unset FIXTURE_CONNECT_MODE
expect_rc "an error line on the hook's stderr is not the result" 0
r=0; json_ok "$out" "done" 'not any(l.get("message") == "on stderr" for l in lines)' || r=1
check "an error line on the hook's stderr is neither relayed nor the result" "$r"
addon disconnect hooked --instance work

: > "$STUB_LOG"
addon_json_in '{"host":"good.example.com","password":"s3cr3t-value"}' connect hooked --instance work --json
expect_rc "connect with a hook" 0
r=0; json_ok "$out" "done" '[s for s in steps if s[1] != "running"] == [("check","done"),("register","done"),("reload","done")] and ("check","running") in steps and last == {"phase":"done","id":"hooked","action":"connect","restart":[],"mcp":["fixture-work"],"account":"work"}' || r=1
check "connect prints check, register and reload, then done with account and mcp" "$r"
r=0; json_ok "$out" "done" 'any(l.get("message") == "checking login" and l["step"] == "check" for l in lines)' || r=1
check "connect relays the hook's progress line inside step check" "$r"
if logged "fixture-connect connect --slug work --json"; then ok "connect runs the hook with --slug work --json"; else bad "connect runs the hook with --slug work --json"; cat "$STUB_LOG"; fi
if [ -f "${HOOKED}/instances/work.json" ] && [ "$(cat "${HOOKED}/secrets/work.password" 2> /dev/null)" = s3cr3t-value ]; then ok "connect registers the instance"; else bad "connect registers the instance"; fi
if reloaded; then ok "connect reloads hermes without a recreate"; else bad "connect reloads hermes without a recreate"; cat "$STUB_LOG"; fi
no_staging "a good connect"

: > "$STUB_LOG"
addon_json_in '{"host":"good.example.com","password":"s3cr3t-value"}' connect hooked --instance work --json
expect_rc "a second connect of the same slug exits 2" 2
r=0; json_ok "$out" error 'last["error"] == "exists"' || r=1
check "a second connect of the same slug is exists" "$r"
if logged "fixture-connect"; then bad "exists is found before the hook runs"; else ok "exists is found before the hook runs"; fi
no_staging "exists"

: > "$STUB_LOG"
addon_json_in '{"host":"h3","password":"s3cr3t-value"}' connect fixture --instance plain --json
expect_rc "connect without a hook" 0
r=0; json_ok "$out" "done" 'steps == [("register","running"),("register","done"),("reload","running"),("reload","done")] and last == {"phase":"done","id":"fixture","action":"connect","restart":[],"mcp":[]}' || r=1
check "connect without a hook prints register and reload, no check" "$r"
if [ -f "${root}/data/addons/fixture/instances/plain.json" ]; then ok "connect without a hook writes the instance"; else bad "connect without a hook writes the instance"; fi
no_staging "a connect without a hook"

addon_in '{"host":"h4","password":"s3cr3t-value"}' connect fixture --instance human
expect_rc "connect without --json" 0
if [ -f "${root}/data/addons/fixture/instances/human.json" ] && ! grep -q '^{' "$out"; then ok "connect without --json writes the instance and prints no JSON"; else bad "connect without --json writes the instance and prints no JSON"; sed 's/^/    /' "$out"; fi

: > "$STUB_LOG"
addon_json disconnect hooked --instance work --json
expect_rc "disconnect with a hook" 0
r=0; json_ok "$out" "done" '[s for s in steps if s[1] != "running"] == [("check","done"),("register","done"),("reload","done")] and last == {"phase":"done","id":"hooked","action":"disconnect","restart":[],"mcp":[]}' || r=1
check "disconnect prints check, register and reload, then done" "$r"
if logged "fixture-connect disconnect --slug work --json"; then ok "disconnect runs the hook with --slug work --json"; else bad "disconnect runs the hook with --slug work --json"; cat "$STUB_LOG"; fi
if [ ! -e "${HOOKED}/instances/work.json" ] && [ ! -e "${HOOKED}/secrets/work.password" ]; then ok "disconnect removes the instance and its secrets"; else bad "disconnect removes the instance and its secrets"; fi
: > "$STUB_LOG"
addon_json disconnect hooked --instance work --json
expect_rc "disconnect of an unknown slug exits 2" 2
r=0; json_ok "$out" error 'last["error"] == "not_found"' || r=1
check "disconnect of an unknown slug is not_found" "$r"
if logged "fixture-connect"; then bad "not_found is found before the hook runs"; else ok "not_found is found before the hook runs"; fi
addon_json disconnect fixture --instance plain --json
r=0; json_ok "$out" "done" 'steps == [("register","running"),("register","done"),("reload","running"),("reload","done")]' || r=1
check "disconnect without a hook prints register and reload" "$r"
addon disconnect fixture --instance human
if [ ! -e "${root}/data/addons/fixture/instances/human.json" ]; then ok "disconnect without --json removes the instance"; else bad "disconnect without --json removes the instance"; fi
addon_json disconnect hooked --json
expect_rc "disconnect without --instance exits 2" 2

# log-tail --redact and redact directly.
mkdir -p "${root}/data/addons"
printf '%s' '{"host":"h","password":"ab\"cd"}' > "${root}/data/addons/.redact-test"
out="${tmp}/out/log-tail-redact"
printf 'x ab"cd y\n{"password":"ab\\"cd"}\nhost h\n' | python3 "${root}/scripts/lib/addon_cli.py" --addons-dir "$ADDONS_DIR" --data-root "${root}/data" log-tail --redact "${root}/data/addons/.redact-test" --sid hooked > "$out" 2>&1 || true
if [ "$(cat "$out")" = '["x [redacted] y","{\"password\":\"[redacted]\"}","host h"]' ]; then ok "log-tail --redact replaces a secret value and its JSON form"; else bad "log-tail --redact replaces a secret value and its JSON form"; sed 's/^/    /' "$out"; fi
rm -f "${root}/data/addons/.redact-test"

# plan-hook and mark-init directly.
plan_hook() { python3 "${root}/scripts/lib/addon_cli.py" --addons-dir "$ADDONS_DIR" --data-root "${root}/data" plan-hook "$@" 2>&1; }
rm -f "$STATE"
if [ "$(plan_hook fixture init)" = "service fixture-worker
fixture-seed-init
--json" ]; then ok "plan-hook prints service, argv and --json"; else bad "plan-hook prints service, argv and --json"; fi
if [ "$(plan_hook hooked init)" = "script
scripts/fixture-init.sh
budget
--json" ]; then ok "plan-hook prints script and its argv"; else bad "plan-hook prints script and its argv"; fi
python3 "${root}/scripts/lib/addon_cli.py" --addons-dir "$ADDONS_DIR" --data-root "${root}/data" mark-init fixture
if [ -z "$(plan_hook fixture init)" ] && [ -n "$(plan_hook fixture init --reinit)" ]; then ok "plan-hook skips a recorded init unless --reinit"; else bad "plan-hook skips a recorded init unless --reinit"; fi
if [ -z "$(plan_hook fixture upgrade)" ]; then ok "plan-hook prints nothing for an absent hook"; else bad "plan-hook prints nothing for an absent hook"; fi
if [ -z "$(plan_hook fixture connect --slug work)" ]; then ok "plan-hook prints nothing for a connect hook the manifest lacks"; else bad "plan-hook prints nothing for a connect hook the manifest lacks"; fi
if plan_hook fixture connect > /dev/null; then bad "connect needs --slug"; else ok "connect needs --slug"; fi
addon disable fixture
addon disable hooked

# With no container exited non-zero there is no target; logTail is the up output.
printf '%s\n%s\n' '{"Service":"fixture-worker","State":"running","ExitCode":0}' '{"Service":"fixture-seed","State":"exited","ExitCode":0}' > "${tmp}/ps-lines.json"
PS_ANSWER="${tmp}/ps-lines.json" DOCKER_FAIL_UP=1 addon_json enable fixture --json
unset PS_ANSWER DOCKER_FAIL_UP
r=0; json_ok "$out" error '"target" not in last and "boom" in " ".join(last["logTail"])' || r=1
check "a failed wait with no failed container has no target" "$r"

# failed-container reads an array or one object per line, one-shots first.
r=0
[ "$(printf '%s' '[{"Service":"a","State":"exited","ExitCode":2},{"Service":"b","State":"exited","ExitCode":3}]' | python3 "${REPO}/scripts/lib/addon_cli.py" --addons-dir "$ADDONS_DIR" --data-root "$tmp" failed-container b,a)" = b ] || r=1
[ "$(printf '%s\n%s\n' '{"Service":"a","State":"exited","ExitCode":0}' '{"Service":"b","State":"exited","ExitCode":3}' | python3 "${REPO}/scripts/lib/addon_cli.py" --addons-dir "$ADDONS_DIR" --data-root "$tmp" failed-container a,b)" = b ] || r=1
[ -z "$(printf '' | python3 "${REPO}/scripts/lib/addon_cli.py" --addons-dir "$ADDONS_DIR" --data-root "$tmp" failed-container a,b)" ] || r=1
if [ "$r" = 0 ]; then ok "failed-container handles both ps formats and orders by the list"; else bad "failed-container handles both ps formats and orders by the list"; fi

addon disable fixture
cp "${root}/.env" "${tmp}/env.before"
: > "$STUB_LOG"
# /bin/sh keeps an assignment made before a function call, hence the unsets.
DOCKER_FAIL_UP=1 addon_json enable fixture --json
unset DOCKER_FAIL_UP
expect_rc "enable --json with compose up failing exits 1" 1
r=0; json_ok "$out" error 'last["error"] == "compose_failed" and last["step"] == "start" and last.get("rolledBack") is True' || r=1
check "a failed start is compose_failed at step start, rolled back" "$r"
# The quotes and backslash are the data, not shell quoting.
# shellcheck disable=SC2089
EXPECT='boom "quoted" \ back'
# shellcheck disable=SC2090
export EXPECT
r=0; json_ok "$out" error 'os.environ["EXPECT"] in last["logTail"] and all(len(t) <= 500 for t in last["logTail"]) and "0" * 500 in last["logTail"] and "colour [31mred[0m end" in last["logTail"]' || r=1
check "logTail keeps the quote and backslash, cuts the long line and drops escapes" "$r"
if cmp -s "${root}/.env" "${tmp}/env.before"; then ok "rollback restores .env"; else bad "rollback restores .env"; diff "${tmp}/env.before" "${root}/.env" | sed 's/^/    /'; fi
if logged "compose stop fixture-worker fixture-extra fixture-seed"; then ok "rollback stops the services of an add-on that was off"; else bad "rollback stops the services of an add-on that was off"; cat "$STUB_LOG"; fi

DOCKER_FAIL_UP=1 addon_json enable fixture
unset DOCKER_FAIL_UP
expect_rc "enable with compose up failing exits 1" 1
if [ ! -s "$out" ] && grep -q '^error: ' "$err"; then ok "a failed enable without --json prints error: on stderr only"; else bad "a failed enable without --json prints error: on stderr only"; sed 's/^/    /' "$out" "$err"; fi
if [ "$(envval FIXTURE_ADDON_ENABLED)" = 0 ]; then ok "a failed enable without --json rolls back"; else bad "a failed enable without --json rolls back"; fi

: > "$STUB_LOG"
DOCKER_FAIL_PULL=1 addon_json enable fixture --json
unset DOCKER_FAIL_PULL
expect_rc "enable --json with the pull failing exits 1" 1
r=0; json_ok "$out" error 'last["error"] == "compose_failed" and last["step"] == "pull" and "rolledBack" not in last' || r=1
check "a failed pull is compose_failed at step pull" "$r"
if cmp -s "${root}/.env" "${tmp}/env.before" && ! logged "compose up"; then ok "a failed pull changes nothing"; else bad "a failed pull changes nothing"; cat "$STUB_LOG"; fi

lock="${root}/data/addons/.lock"
guard="${lock}.takeover"
# lock_target: the PID the lock symlink names, or nothing.
lock_target() { readlink "$lock" 2> /dev/null || true; }
ln -s $$ "$lock"
addon_json enable fixture --json
expect_rc "enable while another change holds the lock exits 1" 1
r=0; json_ok "$out" error 'last["error"] == "busy"' || r=1
check "a held lock is busy" "$r"
if cmp -s "${root}/.env" "${tmp}/env.before" && [ "$(lock_target)" = $$ ]; then
  ok "busy leaves .env and the lock alone"
else
  bad "busy leaves .env and the lock alone"
fi
addon_in '{"interval": 600}' configure fixture
expect_rc "configure while the lock is held exits 1" 1
rm -f "$lock"
ln -s 999999 "$lock"
mkdir "$guard"
addon enable fixture
expect_rc "a dead owner's lock is busy while another takeover holds the guard" 1
if [ "$(lock_target)" = 999999 ] && [ -d "$guard" ]; then ok "a held guard leaves the lock and guard alone"; else bad "a held guard leaves the lock and guard alone"; fi
touch -t 202001010000 "$guard"
addon enable fixture
expect_rc "a guard older than 10 seconds is cleared and the dead owner's lock taken over" 0
if [ ! -e "$lock" ] && [ ! -L "$lock" ] && [ ! -e "$guard" ]; then ok "enable releases the lock and the guard"; else bad "enable releases the lock and the guard"; find "${root}/data/addons" -maxdepth 1 | sed 's/^/    /'; fi
addon disable fixture

# Two takeovers of the same dead owner's lock at once: one gets past it, the
# other is busy.
ln -s 999999 "$lock"
DOCKER_UP_DELAY=1; export DOCKER_UP_DELAY
"$BASH_UNDER_TEST" "${root}/scripts/addon.sh" enable fixture < /dev/null > "${tmp}/out/race1" 2>&1 &
p1=$!
"$BASH_UNDER_TEST" "${root}/scripts/addon.sh" enable fixture < /dev/null > "${tmp}/out/race2" 2>&1 &
p2=$!
rc1=0; wait "$p1" || rc1=$?
rc2=0; wait "$p2" || rc2=$?
unset DOCKER_UP_DELAY
if { [ "$rc1" = 0 ] && [ "$rc2" = 1 ] && grep -q 'another add-on change is running' "${tmp}/out/race2"; } \
  || { [ "$rc1" = 1 ] && [ "$rc2" = 0 ] && grep -q 'another add-on change is running' "${tmp}/out/race1"; }; then
  ok "of two concurrent takeovers one runs and the other is busy"
else
  bad "of two concurrent takeovers one runs and the other is busy: exits ${rc1} and ${rc2}"; sed 's/^/    /' "${tmp}/out/race1" "${tmp}/out/race2"
fi
if [ ! -L "$lock" ] && [ ! -e "$guard" ]; then ok "concurrent takeovers leave no lock or guard"; else bad "concurrent takeovers leave no lock or guard"; fi
addon disable fixture

: > "$STUB_LOG"
export SLEEP_PID="${tmp}/sleep.pid"
rm -f "$SLEEP_PID"
DOCKER_SLEEP_UP=1 "$BASH_UNDER_TEST" "${root}/scripts/addon.sh" enable fixture --json < /dev/null > "${tmp}/out/term" 2> "${tmp}/out/term.err" &
apid=$!
i=0
while [ ! -s "$SLEEP_PID" ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
kill -TERM "$apid" 2> /dev/null || true
rc=0; wait "$apid" || rc=$?
out="${tmp}/out/term"
if [ ! -e "$lock" ] && [ "$(envval FIXTURE_ADDON_ENABLED)" = 0 ]; then
  ok "SIGTERM during start rolls back and releases the lock"
else
  bad "SIGTERM during start rolls back and releases the lock"; sed 's/^/    /' "$out"
fi
r=0; json_ok "$out" error 'last["step"] == "start" and last.get("rolledBack") is True' || r=1
check "SIGTERM ends with an error line" "$r"
if [ -s "$SLEEP_PID" ] && ! kill -0 "$(cat "$SLEEP_PID")" 2>/dev/null; then ok "SIGTERM stops the running compose call"; else bad "SIGTERM stops the running compose call"; fi

# json_str under the C locale: a cut never splits a UTF-8 sequence, and C1
# controls are dropped as clean_line drops them.
events_c() {
  # shellcheck disable=SC2016 # expanded by the inner bash
  env -u LC_ALL -u LC_CTYPE LANG= "$BASH_UNDER_TEST" -c '. "$1"; json_str "$2"' _ "${REPO}/scripts/lib/addon-events.sh" "$1"
}
long_e="a$(python3 -c 'print("\u00e9" * 600, end="")')"
out="${tmp}/out/json-str"
events_c "$long_e" > "$out" 2>&1 || true
if python3 -c 'import json, sys; t = json.loads(open(sys.argv[1], "rb").read().decode("utf-8")); sys.exit(0 if t.startswith("a\u00e9") and set(t[1:]) == {"\u00e9"} and len(t.encode()) <= 500 else 1)' "$out" 2> /dev/null; then
  ok "json_str cuts 600 x e-acute without splitting a UTF-8 sequence"
else
  bad "json_str cuts 600 x e-acute without splitting a UTF-8 sequence"
fi
events_c "$(printf 'x\302\205y\033z')" > "$out" 2>&1 || true
if [ "$(cat "$out")" = '"xyz"' ]; then ok "json_str drops C1 and ASCII controls"; else bad "json_str drops C1 and ASCII controls"; sed 's/^/    /' "$out"; fi
events_c "$(printf 'a\377b\303(c\355\240\200d\303\251')" > "$out" 2>&1 || true
if python3 -c 'import json, sys; t = json.loads(open(sys.argv[1], "rb").read().decode("utf-8")); sys.exit(0 if t == "a\ufffdb\ufffd(c\ufffd\ufffd\ufffdd\u00e9" else 1)' "$out" 2> /dev/null; then
  ok "json_str replaces invalid UTF-8 bytes with U+FFFD"
else
  bad "json_str replaces invalid UTF-8 bytes with U+FFFD"; sed 's/^/    /' "$out"
fi

cli_host() { python3 "${REPO}/scripts/lib/addon_cli.py" --addons-dir "$ADDONS_DIR" --data-root "$tmp" "$@"; }
out="${tmp}/out/pull-events"
cli_host pull-events bb < "${REPO}/scripts/fixtures/compose-progress-pull.jsonl" > "$out" 2>&1 || true
r=0; json_ok "$out" pull 'last == {"phase":"pull","service":"bb","status":"done"} and any(l.get("total") == 1896265 for l in lines)' || r=1
check "pull-events reads compose progress and ends with done per service" "$r"
out="${tmp}/out/log-tail"
printf 'one\ntwo\tx\nthree\n' | cli_host log-tail --lines 2 > "$out" 2>&1 || true
if [ "$(cat "$out")" = '["two x","three"]' ]; then ok "log-tail prints the last lines as a JSON array"; else bad "log-tail prints the last lines as a JSON array"; sed 's/^/    /' "$out"; fi

addon enable fixture

for f in "${root}/.env" "$STUB_LOG" "${tmp}"/out/*; do
  if grep -q -e s3cr3t-value -e tok3n-value "$f"; then bad "secret value appears in ${f#"${tmp}/"}"; fi
done
grep -r -q -e s3cr3t-value -e tok3n-value "${root}/.env" "$STUB_LOG" "${tmp}/out" || ok "no secret value in .env, stub logs or output"
# Under the scratch root, a secret value may sit only in data/addons/<id>/secrets.
leaks="$(grep -r -l -e s3cr3t-value -e tok3n-value "$root" "${tmp}/hosttmp" 2> /dev/null | grep -v -E "^${root}/data/addons/[^/]+/secrets/" || true)"
if [ -z "$leaks" ]; then ok "no secret value in any file outside data/addons/<id>/secrets"; else bad "secret value in: ${leaks}"; fi

# A failure inside addon_cli.py is one error line, exit 1, no traceback.
mkdir -p "${root}/data/addons/fixture/locked"
: > "${root}/data/addons/fixture/locked/file"
chmod 500 "${root}/data/addons/fixture/locked"
addon disable fixture --purge
chmod 700 "${root}/data/addons/fixture/locked"
expect_rc "a purge that cannot delete exits 4 (command_failed)" 4
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

# Reload failures, onToggle, done.mcp and the stop of unused `uses` services.
: > "$STUB_LOG"
VAULT_FAIL=1 addon_json enable hooked --json
unset VAULT_FAIL
expect_rc "a failing reload-plugins exits 4" 4
r=0; json_ok "$out" error 'last["error"] == "command_failed" and last["step"] == "reload" and last.get("rolledBack") is True' || r=1
check "a failing reload-plugins is command_failed at step reload, rolled back" "$r"
if [ "$(envval HOOKED_ADDON_ENABLED)" != 1 ]; then ok "a failing reload-plugins restores the flag"; else bad "a failing reload-plugins restores the flag"; fi

: > "$STUB_LOG"
DOCKER_FAIL_BOOTSTRAP=1 addon_json enable hooked --json
unset DOCKER_FAIL_BOOTSTRAP
expect_rc "a failing bootstrap exits 1" 1
r=0; json_ok "$out" error 'last["error"] == "compose_failed" and last["step"] == "reload" and last.get("rolledBack") is True' || r=1
check "a failing bootstrap is compose_failed at step reload" "$r"
if logged "vault.sh"; then bad "a failing bootstrap skips reload-plugins"; else ok "a failing bootstrap skips reload-plugins"; fi

: > "$STUB_LOG"
addon_json enable toggled --json
expect_rc "enable toggled" 0
r=0; json_ok "$out" "done" 'last == {"phase":"done","id":"toggled","action":"enable","restart":["hermes"],"mcp":["toggled-mcp"]} and ("recreate","done") in steps' || r=1
check "enable with onToggle recreates hermes and reports its mcp name" "$r"
if logged "compose up -d --no-deps --force-recreate hermes" && logged "fixture-follow follow"; then
  ok "the first enable recreates hermes and runs the onToggle script"
else
  bad "the first enable recreates hermes and runs the onToggle script"; cat "$STUB_LOG"
fi
rec_at="$(grep -n 'force-recreate' "$STUB_LOG" | head -1 | cut -d: -f1 || true)"
rel_at="$(grep -n 'vault.sh reload-plugins' "$STUB_LOG" | head -1 | cut -d: -f1 || true)"
if [ -n "$rec_at" ] && [ -n "$rel_at" ] && [ "$rec_at" -lt "$rel_at" ]; then
  ok "the recreate runs before the reload"
else
  bad "the recreate runs before the reload"
fi
: > "$STUB_LOG"
addon_json enable toggled --json
r=0; json_ok "$out" "done" 'last["restart"] == [] and ("recreate","running") not in steps' || r=1
check "a second enable while enabled recreates nothing" "$r"
if logged "force-recreate" || logged "fixture-follow"; then bad "a second enable logs no recreate or script"; else ok "a second enable logs no recreate or script"; fi

# uses: disable stops neo4j only when no active profile keeps it in the config.
printf 'toggled-worker\nneo4j\n' > "${tmp}/services-with"
printf 'toggled-worker\nhermes\n' > "${tmp}/services-without"
: > "$STUB_LOG"
CONFIG_SERVICES="${tmp}/services-with" addon_json disable toggled --json
r=0; json_ok "$out" "done" 'last == {"phase":"done","id":"toggled","action":"disable","restart":["hermes"],"mcp":[]} and ("recreate","done") in steps' || r=1
check "disable with onToggle recreates hermes and reports no mcp" "$r"
if logged "compose stop neo4j"; then bad "disable leaves a uses service that config still lists"; else ok "disable leaves a uses service that config still lists"; fi
if logged "compose stop toggled-worker"; then ok "disable stops the add-on's own service"; else bad "disable stops the add-on's own service"; fi
addon enable toggled
: > "$STUB_LOG"
CONFIG_SERVICES="${tmp}/services-without" addon_json disable toggled --json
if logged "compose stop neo4j"; then ok "disable stops a uses service that config no longer lists"; else bad "disable stops a uses service that config no longer lists"; cat "$STUB_LOG"; fi
r=0; json_ok "$out" "done" 'any(l.get("step") == "stop" and l.get("target") == "neo4j" for l in lines)' || r=1
check "the uses stop names its target" "$r"
addon enable toggled
: > "$STUB_LOG"
addon disable fixture
if logged "neo4j"; then bad "an add-on without uses stops no stack service"; else ok "an add-on without uses stops no stack service"; fi

# Disabling an add-on that is already off recreates nothing.
addon disable toggled
: > "$STUB_LOG"
addon_json disable toggled --json
expect_rc "disable an already disabled toggled" 0
r=0; json_ok "$out" "done" 'last["restart"] == [] and ("recreate","running") not in steps' || r=1
check "disable while disabled recreates nothing" "$r"
if logged "force-recreate" || logged "fixture-follow"; then bad "disable while disabled logs no recreate or script"; else ok "disable while disabled logs no recreate or script"; fi

# Recreate failures roll the enable back.
: > "$STUB_LOG"
DOCKER_FAIL_RECREATE=1 addon_json enable toggled --json
unset DOCKER_FAIL_RECREATE
expect_rc "a failing recreate exits 1" 1
r=0; json_ok "$out" error 'last["error"] == "compose_failed" and last["step"] == "recreate" and last.get("rolledBack") is True and "recreate failed" in last["logTail"]' || r=1
check "a failing recreate is compose_failed at step recreate, rolled back" "$r"
if [ "$(envval TOGGLED_ADDON_ENABLED)" != 1 ]; then ok "a failing recreate restores the flag"; else bad "a failing recreate restores the flag"; fi
if logged "fixture-follow"; then bad "a failing recreate skips the script"; else ok "a failing recreate skips the script"; fi

: > "$STUB_LOG"
FIXTURE_FOLLOW_FAIL=1 addon_json enable toggled --json
unset FIXTURE_FOLLOW_FAIL
expect_rc "a failing onToggle script exits 4" 4
r=0; json_ok "$out" error 'last["error"] == "command_failed" and last["step"] == "recreate" and last.get("rolledBack") is True' || r=1
check "a failing onToggle script is command_failed at step recreate, rolled back" "$r"
if [ "$(envval TOGGLED_ADDON_ENABLED)" != 1 ]; then ok "a failing onToggle script restores the flag"; else bad "a failing onToggle script restores the flag"; fi

# A uses name with no compose mapping is never stopped.
printf 'pguser-worker\n' > "${tmp}/services-none"
addon enable pguser
: > "$STUB_LOG"
CONFIG_SERVICES="${tmp}/services-none" addon_json disable pguser --json
expect_rc "disable pguser" 0
if logged "compose stop pguser-worker" && ! logged "postgres"; then ok "an unmapped uses name is never stopped"; else bad "an unmapped uses name is never stopped"; cat "$STUB_LOG"; fi

# remove --delete-data. Every path these checks delete is under $root, except
# for the ASSISTANT_DATA_ROOT case under ${tmp}/dataroot. The `/` and `..`
# cases run remove only after plan-remove has reported the path as kept.
export TEST_ROOT="$root" TEST_TMP="$tmp"
set_env() {
  grep -v -E "^$1=" "${root}/.env" > "${tmp}/env.new" || true
  printf '%s=%s\n' "$1" "$2" >> "${tmp}/env.new"
  mv "${tmp}/env.new" "${root}/.env"
}
unset_env() {
  grep -v -E "^$1=" "${root}/.env" > "${tmp}/env.new" || true
  mv "${tmp}/env.new" "${root}/.env"
}
# plan_remove ENV_JSON FLAGS_JSON: plan-remove fixture, its output in $out.
plan_remove() {
  n=$((n + 1))
  out="${tmp}/out/${n}"
  python3 "${root}/scripts/lib/addon_cli.py" --addons-dir "$ADDONS_DIR" --data-root "${root}/data" \
    plan-remove fixture --env "$1" --flags "$2" --repo "$root" > "$out" 2>&1 || true
}
plan_is() { python3 -c 'import json, os, sys
p = json.load(open(sys.argv[1]))
r, t = os.environ["TEST_ROOT"], os.environ["TEST_TMP"]
sys.exit(0 if eval(sys.argv[2]) else 1)' "$out" "$1" 2> /dev/null; }
seed_data() {
  mkdir -p "${root}/data/fixture/sub" "${root}/data/addons/fixture/secrets" "${root}/data/other"
  : > "${root}/data/fixture/sub/file"
  : > "${root}/data/addons/fixture/secrets/token"
  : > "${root}/data/other/keep"
}
addon disable fixture
unset FIXTURE_DATA_DIR
unset_env FIXTURE_DATA_DIR

n=$((n + 1)); out="${tmp}/out/${n}"
python3 "${root}/scripts/lib/addon_cli.py" --addons-dir "$ADDONS_DIR" --data-root "${root}/data" data-envs fixture > "$out" 2>&1 || true
if grep -qx FIXTURE_DATA_DIR "$out"; then ok "data-envs names the env of each data path"; else bad "data-envs names the env of each data path"; sed 's/^/    /' "$out"; fi
plan_remove '{}' '{}'
r=0; plan_is 'p == {"services": ["fixture-seed", "fixture-worker", "fixture-extra"], "volumes": ["fixture_cache"], "delete": [r + "/data/fixture", r + "/data/addons/fixture"], "kept": []}' || r=1
check "plan-remove lists services, volumes and the paths to delete" "$r"
plan_remove '{}' '{"FIXTURE_ADDON_ENABLED":"1"}'
if grep -q '^error: .*enabled' "$out"; then ok "plan-remove refuses an enabled add-on"; else bad "plan-remove refuses an enabled add-on"; sed 's/^/    /' "$out"; fi

seed_data
mkdir -p "${tmp}/outside-in"
: > "${tmp}/outside-in/file"
ln -s "${tmp}/outside-in" "${root}/data/fixture/link"
: > "$STUB_LOG"
addon_json remove fixture --delete-data --json
expect_rc "remove fixture --delete-data" 0
r=0; json_ok "$out" "done" 'last == {"phase":"done","id":"fixture","action":"remove","restart":[],"mcp":[]} and [l.get("target") for l in lines if l["phase"] == "setup" and l["status"] == "done" and l["step"] == "delete"] == [None, "fixture_cache", os.environ["TEST_ROOT"] + "/data/fixture", os.environ["TEST_ROOT"] + "/data/addons/fixture"]' || r=1
check "remove prints a delete step for the services, each volume and each path" "$r"
if [ ! -e "${root}/data/fixture" ] && [ ! -e "${root}/data/addons/fixture" ]; then ok "remove deletes data/fixture and data/addons/fixture"; else bad "remove deletes data/fixture and data/addons/fixture"; fi
if [ -f "${root}/data/other/keep" ] && [ -d "${root}/data/addons" ]; then ok "remove leaves the rest of data/"; else bad "remove leaves the rest of data/"; fi
if [ -f "${tmp}/outside-in/file" ]; then ok "remove does not follow a symlink inside a deleted directory"; else bad "remove does not follow a symlink inside a deleted directory"; fi
rm_at="$(grep -n -x 'compose rm -f -s fixture-seed fixture-worker fixture-extra' "$STUB_LOG" | head -1 | cut -d: -f1 || true)"
vol_at="$(grep -n -x 'volume rm fixtureproj_fixture_cache' "$STUB_LOG" | head -1 | cut -d: -f1 || true)"
if [ -n "$rm_at" ] && [ -n "$vol_at" ] && [ "$rm_at" -lt "$vol_at" ]; then ok "remove runs compose rm -f -s before volume rm"; else bad "remove runs compose rm -f -s before volume rm"; cat "$STUB_LOG"; fi

# Values that point at or above the data directory are kept.
for v in / .. ./data; do
  case "$v" in
    /) want="/"; reason="outside the data directory" ;;
    ..) want="$tmp"; reason="outside the data directory" ;;
    *) want="${root}/data"; reason="is the data directory" ;;
  esac
  set_env FIXTURE_DATA_DIR "$v"
  seed_data
  plan_remove "{\"FIXTURE_DATA_DIR\":\"${v}\"}" '{}'
  if ! WANT="$want" REASON="$reason" plan_is 'p["kept"] == [{"path": os.environ["WANT"], "reason": os.environ["REASON"]}] and p["delete"] == [r + "/data/addons/fixture"]'; then
    bad "FIXTURE_DATA_DIR=${v} is kept by plan-remove (remove not run)"; sed 's/^/    /' "$out"
    continue
  fi
  addon_json remove fixture --delete-data --json
  expect_rc "remove with FIXTURE_DATA_DIR=${v}" 0
  r=0; WANT="$want" REASON="$reason" json_ok "$out" "done" '{"phase":"setup","step":"delete","status":"done","kept":os.environ["WANT"],"reason":os.environ["REASON"]} in lines' || r=1
  check "FIXTURE_DATA_DIR=${v} is reported as kept: ${reason}" "$r"
  if [ ! -e "${root}/data/addons/fixture" ] && [ -f "${root}/data/fixture/sub/file" ] && [ -f "${root}/data/other/keep" ] \
    && [ -f "${root}/.env" ] && [ -f "${tmp}/outside-in/file" ]; then
    ok "FIXTURE_DATA_DIR=${v} deletes only data/addons/fixture"
  else
    bad "FIXTURE_DATA_DIR=${v} deletes only data/addons/fixture"
  fi
done
unset_env FIXTURE_DATA_DIR

# data/fixture as a symlink to a directory outside the scratch root.
rm -rf "${root}/data/fixture"
mkdir -p "${tmp}/outside-link"
: > "${tmp}/outside-link/file"
ln -s "${tmp}/outside-link" "${root}/data/fixture"
addon_json remove fixture --delete-data --json
expect_rc "remove with data/fixture a symlink" 0
if [ ! -L "${root}/data/fixture" ] && [ ! -e "${root}/data/fixture" ] && [ -f "${tmp}/outside-link/file" ]; then
  ok "a symlinked data path is unlinked, not followed"
else
  bad "a symlinked data path is unlinked, not followed"
fi

# A path under a symlinked directory is kept.
mkdir -p "${tmp}/outside-behind/sub"
: > "${tmp}/outside-behind/sub/file"
ln -s "${tmp}/outside-behind" "${root}/data/lnk"
set_env FIXTURE_DATA_DIR ./data/lnk/sub
addon_json remove fixture --delete-data --json
r=0; json_ok "$out" "done" '{"phase":"setup","step":"delete","status":"done","kept":os.environ["TEST_ROOT"] + "/data/lnk/sub","reason":"behind a symlink"} in lines' || r=1
check "a path behind a symlink is kept" "$r"
if [ -f "${tmp}/outside-behind/sub/file" ] && [ -L "${root}/data/lnk" ]; then ok "a path behind a symlink is not deleted"; else bad "a path behind a symlink is not deleted"; fi
rm -f "${root}/data/lnk"

# Hermes state and data/addons are kept.
mkdir -p "${root}/data/hermes"
: > "${root}/data/hermes/keep"
for v in ./data/hermes ./data/addons; do
  set_env FIXTURE_DATA_DIR "$v"
  seed_data
  addon_json remove fixture --delete-data --json
  r=0; WANT="${root}/${v#./}" json_ok "$out" "done" '{"phase":"setup","step":"delete","status":"done","kept":os.environ["WANT"],"reason":"holds stack data"} in lines' || r=1
  check "FIXTURE_DATA_DIR=${v} is kept as stack data" "$r"
done
if [ -f "${root}/data/hermes/keep" ] && [ -d "${root}/data/addons" ]; then ok "data/hermes and data/addons survive"; else bad "data/hermes and data/addons survive"; fi

# The caller's environment comes before .env.
set_env FIXTURE_DATA_DIR ./data/fixture
seed_data
mkdir -p "${root}/data/alt"
: > "${root}/data/alt/file"
FIXTURE_DATA_DIR=./data/alt addon_json remove fixture --delete-data --json
unset FIXTURE_DATA_DIR
if [ ! -e "${root}/data/alt" ] && [ -f "${root}/data/fixture/sub/file" ]; then ok "the environment's FIXTURE_DATA_DIR wins over .env"; else bad "the environment's FIXTURE_DATA_DIR wins over .env"; fi
unset_env FIXTURE_DATA_DIR

# A quote and a backslash in the value survive the trip to plan-remove; a
# control character is refused before anything is deleted.
seed_data
mkdir -p "${root}/data/q\"b\\s"
set_env FIXTURE_DATA_DIR './data/q"b\s'
addon_json remove fixture --delete-data --json
if [ "$rc" = 0 ] && [ ! -e "${root}/data/q\"b\\s" ] && [ -f "${root}/data/fixture/sub/file" ]; then
  ok "a path with a quote and a backslash is deleted"
else
  bad "a path with a quote and a backslash is deleted"; sed 's/^/    /' "$out" "$err"
fi
unset_env FIXTURE_DATA_DIR
seed_data
FIXTURE_DATA_DIR="$(printf './data/a\tb')" addon_json remove fixture --delete-data --json
unset FIXTURE_DATA_DIR
r=0; json_ok "$out" error 'last["error"] == "bad_input"' || r=1
if [ "$rc" = 2 ] && [ "$r" = 0 ] && [ -f "${root}/data/addons/fixture/secrets/token" ]; then
  ok "a control character in a data path is bad_input and deletes nothing"
else
  bad "a control character in a data path is bad_input and deletes nothing"; sed 's/^/    /' "$out"
fi

# A path another enabled add-on declares is kept.
seed_data
set_env SHARER_ADDON_ENABLED 1
addon_json remove fixture --delete-data --json
r=0; json_ok "$out" "done" '{"phase":"setup","step":"delete","status":"done","kept":os.environ["TEST_ROOT"] + "/data/fixture","reason":"used by sharer"} in lines' || r=1
check "a path an enabled add-on declares is kept, used by sharer" "$r"
if [ -f "${root}/data/fixture/sub/file" ] && [ ! -e "${root}/data/addons/fixture" ]; then ok "the shared path survives"; else bad "the shared path survives"; fi
set_env SHARER_ADDON_ENABLED 0
addon_json remove fixture --delete-data --json
if [ ! -e "${root}/data/fixture" ]; then ok "a disabled add-on's declaration keeps nothing"; else bad "a disabled add-on's declaration keeps nothing"; fi
unset_env SHARER_ADDON_ENABLED

# ASSISTANT_DATA_ROOT outside the repo is an allowed root too.
mkdir -p "${tmp}/dataroot/fixture" "${tmp}/dataroot/addons/fixture"
: > "${tmp}/dataroot/fixture/file"
set_env ASSISTANT_DATA_ROOT "${tmp}/dataroot"
set_env FIXTURE_DATA_DIR "${tmp}/dataroot/fixture"
addon_json remove fixture --delete-data --json
expect_rc "remove under ASSISTANT_DATA_ROOT" 0
if [ ! -e "${tmp}/dataroot/fixture" ] && [ ! -e "${tmp}/dataroot/addons/fixture" ] && [ -d "${tmp}/dataroot/addons" ]; then
  ok "remove deletes paths under ASSISTANT_DATA_ROOT"
else
  bad "remove deletes paths under ASSISTANT_DATA_ROOT"; sed 's/^/    /' "$out" "$err"
fi
set_env FIXTURE_DATA_DIR "${tmp}/dataroot"
addon_json remove fixture --delete-data --json
r=0; json_ok "$out" "done" '{"phase":"setup","step":"delete","status":"done","kept":os.environ["TEST_TMP"] + "/dataroot","reason":"is the data directory"} in lines' || r=1
check "ASSISTANT_DATA_ROOT itself is kept" "$r"
unset_env FIXTURE_DATA_DIR
unset_env ASSISTANT_DATA_ROOT

# Refusals.
seed_data
set_env FIXTURE_ADDON_ENABLED 1
: > "$STUB_LOG"
addon_json remove fixture --delete-data --json
expect_rc "remove while enabled exits 2" 2
r=0; json_ok "$out" error 'last["error"] == "bad_input"' || r=1
check "remove while enabled is bad_input" "$r"
if [ -f "${root}/data/addons/fixture/secrets/token" ] && ! logged "compose rm"; then ok "remove while enabled deletes nothing"; else bad "remove while enabled deletes nothing"; fi
set_env FIXTURE_ADDON_ENABLED 0
addon remove fixture
expect_rc "remove without --delete-data exits 2" 2
addon remove
expect_rc "remove without a short id exits 2" 2
addon remove fixture --delete-data --force
expect_rc "remove with an unknown option exits 2" 2
addon remove .. --delete-data
expect_rc "remove .. exits 2" 2
if [ -f "${root}/data/addons/fixture/secrets/token" ]; then ok "refused removes delete nothing"; else bad "refused removes delete nothing"; fi

# delete-path checks again what it is given.
mkdir -p "${root}/data/hermes" "${root}/data/fixture"
: > "${root}/data/hermes/keep"
for p in "${root}/data" "${root}/data/hermes" "${root}/data/fixture/../hermes" "${root}/data/addons"; do
  r=0
  python3 "${root}/scripts/lib/addon_cli.py" --addons-dir "$ADDONS_DIR" --data-root "${root}/data" \
    delete-path "$p" --repo "$root" > /dev/null 2>&1 || r=$?
  if [ "$r" = 2 ]; then ok "delete-path refuses ${p#"${root}/"}"; else bad "delete-path refuses ${p#"${root}/"}: exit ${r}, want 2"; fi
done
if [ -f "${root}/data/hermes/keep" ] && [ -d "${root}/data/fixture" ]; then ok "a refused delete-path deletes nothing"; else bad "a refused delete-path deletes nothing"; fi

# disable --purge: one lock, one done line with action disable.
seed_data
addon_json disable fixture --purge --json
expect_rc "disable --purge --json" 0
r=0; json_ok "$out" "done" 'last["action"] == "disable" and sum(l["phase"] == "done" for l in lines) == 1 and ("delete","done") in steps' || r=1
check "disable --purge prints one done line, action disable" "$r"
if [ ! -e "${root}/data/fixture" ] && [ ! -e "${root}/data/addons/fixture" ]; then ok "disable --purge deletes the data paths"; else bad "disable --purge deletes the data paths"; fi

if [ "$fail" = 0 ]; then
  echo "all addon.sh checks passed"
else
  exit 1
fi
