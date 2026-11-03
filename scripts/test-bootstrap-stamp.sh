#!/bin/sh
# hermes-*-bootstrap: when the stamp skips a run, and what a full run spawns.
#
# Runs the whole bootstrap against a scratch HERMES_HOME with stub `hermes` and
# `python3` on PATH. The stubs write no config, so a second run sees the same
# state as the first. The python3 stub passes `-c` through to the real one
# (sha256_stdin's fallback) and records each config_flush queue.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
REAL_PYTHON="$(command -v python3)"
# The image's /bin/sh is dash; use it here too when it is installed.
BOOTSTRAP_SH="${BOOTSTRAP_SH:-$(command -v dash || echo sh)}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
home="${tmp}/home"
mkdir -p "${tmp}/bin" "${tmp}/bootstrap" "${tmp}/caldav" \
  "${home}/profiles/api-server" "${home}/profiles/browser" "${home}/profiles/worker"
printf 'model:\n  default: test\n' > "${home}/config.yaml"
printf 'API_SERVER_KEY=api-key\n' > "${tmp}/bootstrap/api-server.env"
printf 'API_SERVER_KEY=browser-key\n' > "${tmp}/bootstrap/browser.env"

cat > "${tmp}/bin/hermes" <<EOF
#!/bin/sh
echo "hermes \$*" >> "${tmp}/spawns"
case "\$*" in
  "profile list") echo "api-server browser worker" ;;
esac
exit 0
EOF

cat > "${tmp}/bin/python3" <<EOF
#!/bin/sh
[ "\${1:-}" = "-c" ] && exec "$REAL_PYTHON" "\$@"
script="\$(cat)"
case "\$script" in
  *set_config_value*)
    echo "flush" >> "${tmp}/spawns"
    cat "\$3" >> "${tmp}/queue"
    exit "\${FLUSH_EXIT:-0}"
    ;;
esac
echo "python3" >> "${tmp}/spawns"
exit 0
EOF
chmod +x "${tmp}/bin/hermes" "${tmp}/bin/python3"

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

# run <profile> [VAR=value...]; leaves the log in $tmp/out
run() {
  _profile="$1"
  shift
  : > "${tmp}/spawns"
  : > "${tmp}/queue"
  _shared=1
  [ "$_profile" = browser ] && _shared=0
  _status=0
  env -i PATH="${tmp}/bin:/usr/bin:/bin:/usr/local/bin" \
    HERMES_HOME="$home" \
    HERMES_API_PROFILE_NAME="$_profile" \
    BOOTSTRAP_API_ENV="${tmp}/bootstrap/${_profile}.env" \
    CALDAV_MCP_ACCOUNTS_DIR="${tmp}/caldav" \
    HERMES_BOOTSTRAP_SHARED="$_shared" \
    HERMES_AGENT_IMAGE_TAG=v0.22.5 \
    "$@" \
    "$BOOTSTRAP_SH" "$bootstrap" > "${tmp}/out" 2>&1 || _status=$?
  return "$_status"
}

skipped() { grep -q 'unchanged since the last run; skipping' "${tmp}/out"; }

# expect_skip|expect_full <message> <profile> [VAR=value...]
expect_skip() {
  _msg="$1"
  shift
  run "$@" || fail "${_msg} (exited non-zero)"
  skipped || fail "$_msg"
}
expect_full() {
  _msg="$1"
  shift
  run "$@" || fail "${_msg} (exited non-zero)"
  if skipped; then fail "$_msg"; fi
}
count() { grep -c "^$1" "${tmp}/spawns" || true; }

# A first run is full, sets every profile's keys in one flush per batch, and
# calls the hermes CLI only to list profiles.
run api-server || fail "first api-server run exited non-zero: $(tail -5 "${tmp}/out")"
skipped && fail "first run skipped"
[ "$(count hermes)" = 1 ] || fail "expected one hermes call (profile list), got: $(grep ^hermes "${tmp}/spawns" | tr '\n' ';')"
for profile in "" api-server worker; do
  grep -q "^${profile}	terminal.backend	ssh$" "${tmp}/queue" || fail "terminal.backend not queued for '${profile:-default}'"
done
grep -q "^api-server	agent.max_turns	20$" "${tmp}/queue" || fail "agent.max_turns not queued"
[ -s "${home}/.bootstrap-stamps/api-server" ] || fail "no api-server stamp"
[ -s "${home}/.bootstrap-stamps/state" ] || fail "no state stamp"
grep -q '"running"' "${home}/profiles/api-server/gateway_state.json" || fail "gateway not marked running"

# Unchanged: skipped, nothing spawned, gateway still marked.
rm -f "${home}/profiles/api-server/gateway_state.json"
run api-server || fail "second api-server run exited non-zero"
skipped || fail "unchanged run was not skipped: $(tail -5 "${tmp}/out")"
if ! { [ "$(count hermes)" = 0 ] && [ "$(count flush)" = 0 ] && [ "$(count python3)" = 0 ]; }; then
  fail "skipped run spawned: $(tr '\n' ';' < "${tmp}/spawns")"
fi
grep -q '"running"' "${home}/profiles/api-server/gateway_state.json" || fail "skipped run did not mark the gateway"

# The browser bootstrap touches its own profile only.
run browser || fail "first browser run exited non-zero: $(tail -5 "${tmp}/out")"
skipped && fail "first browser run skipped"
others="$(grep -v '^browser	' "${tmp}/queue" || true)"
[ -z "$others" ] || fail "browser run queued keys for other profiles: $(echo "$others" | cut -f1,2 | tr '\n' ';')"
grep -q '^browser	model.max_tokens	1024$' "${tmp}/queue" || fail "browser model.max_tokens not queued"

# Both are skipped after the browser run recorded the shared state.
expect_skip "api-server not skipped after the browser run" api-server
expect_skip "browser not skipped on its second run" browser

# A changed input runs the full bootstrap.
run api-server HERMES_API_MAX_TURNS=21 || fail "changed-env run exited non-zero"
skipped && fail "changed environment was skipped"
grep -q "^api-server	agent.max_turns	21$" "${tmp}/queue" || fail "changed agent.max_turns not queued"
run api-server || fail "api-server rerun exited non-zero"
skipped && fail "reverted environment was skipped"
expect_skip "api-server not skipped after re-stamping" api-server

printf 'API_SERVER_KEY=rotated\n' > "${tmp}/bootstrap/api-server.env"
expect_full "changed api-server.env was skipped" api-server
printf 'URL=https://dav.example.com\n' > "${tmp}/caldav/personal.env"
expect_full "new CalDAV account was skipped" api-server

# Config changed outside the bootstrap, or a profile added: full run.
expect_skip "api-server not skipped before the config edit" api-server
printf 'model:\n  default: edited\n' > "${home}/config.yaml"
expect_full "hand-edited config.yaml was skipped" api-server
mkdir -p "${home}/profiles/new-worker"
expect_full "new profile was skipped" api-server
grep -q "^new-worker	terminal.backend	ssh$" "${tmp}/queue" || fail "new profile not swept"

# Forced.
expect_full "HERMES_BOOTSTRAP_FORCE=1 was skipped" api-server HERMES_BOOTSTRAP_FORCE=1

# A failed flush fails the run and leaves no stamp, so the next start retries.
run api-server HERMES_BOOTSTRAP_FORCE=1 FLUSH_EXIT=1 && fail "failed flush exited zero"
[ -e "${home}/.bootstrap-stamps/api-server" ] && fail "failed run left a stamp"
expect_full "run after a failed one was skipped" api-server

printf 'OK bootstrap stamp\n'
