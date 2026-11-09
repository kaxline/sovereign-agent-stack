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
# DOCKER_FAIL_ONESHOT=1 makes `compose up --no-deps --exit-code-from` exit 1.
# `compose ps -a --format json` prints the file PS_ANSWER names, if any.
# `compose logs` prints one line naming the service.
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
if [ "$1 $2" = "compose logs" ]; then
  eval "last=\${$#}"
  echo "log line from $last"
  exit 0
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

if [ "$fail" = 0 ]; then
  echo "all addon.sh checks passed"
else
  exit 1
fi
