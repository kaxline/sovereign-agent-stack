#!/bin/sh
# Gateway add-ons (addons/signal, addons/buzz) through scripts/addon.sh: enable
# and disable run gateway.sync after writing the flag, sync-profiles.sh owns
# the compose profile, and a failed sync rolls the flag back. Runs in a
# scratch ROOT with host python3 and stub docker; Signal data is synthetic
# (+1 555-01xx numbers, an empty scratch SIGNAL_CLI_DATA_DIR) and nothing is
# ever sent.
#
#   sh scripts/test-addon-gateway.sh
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BASH_UNDER_TEST=bash
[ -x /bin/bash ] && BASH_UNDER_TEST=/bin/bash

tmp="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$tmp"' EXIT
root="${tmp}/stack"
mkdir -p "${root}/scripts/lib" "${tmp}/bin" "${tmp}/out" "${tmp}/synthetic-signal"
for f in addon.sh sync-profiles.sh sync-signal-profile.sh sync-buzz-profile.sh; do
  cp "${REPO}/scripts/${f}" "${root}/scripts/"
done
for f in env.sh data-root.sh python.sh addon-events.sh addon_cli.py addon_manifest.py addon_store.py hermes-yaml.sh; do
  cp "${REPO}/scripts/lib/${f}" "${root}/scripts/lib/"
done
cp -R "${REPO}/addons" "${root}/addons"
cp "${REPO}/docker-compose.yml" "${root}/docker-compose.yml"

# docker stub: logs argv, one call per line, and succeeds.
cat > "${tmp}/bin/docker" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "${STUB_LOG}"
exit 0
EOF
chmod +x "${tmp}/bin/docker"
cat > "${root}/scripts/vault.sh" <<'EOF'
#!/bin/sh
printf 'vault.sh %s\n' "$*" >> "${STUB_LOG}"
EOF
chmod +x "${root}/scripts/vault.sh"

export PATH="${tmp}/bin:${PATH}" STUB_LOG="${tmp}/docker.log" STACK_PYTHON=host
: > "$STUB_LOG"
cd "$root"

mkdir -p data/hermes/profiles/browser data/hermes/profiles/engineer
: > data/hermes/profiles/browser/config.yaml
: > data/hermes/profiles/engineer/config.yaml
cat > .env <<EOF
COMPOSE_PROJECT_NAME=gwtest
COMPOSE_PROFILES=core
HERMES_SIGNAL_ENABLED=0
HERMES_BUZZ_ENABLED=0
HERMES_BUZZ_PROFILES=engineer
BUZZ_RELAY_URL=ws://localhost:3000
SIGNAL_CLI_DATA_DIR=${tmp}/synthetic-signal
SIGNAL_CLI_UID=501
SIGNAL_CLI_GID=20
EOF

fails=0
ok() { echo "ok: $1"; }
bad() { echo "FAIL: $1"; fails=$((fails + 1)); }
n=0
addon() {
  n=$((n + 1))
  out="${tmp}/out/${n}"
  rc=0
  "$BASH_UNDER_TEST" scripts/addon.sh "$@" > "$out" 2>&1 || rc=$?
}
expect_rc() {
  if [ "$rc" = "$2" ]; then ok "$1"; else bad "$1: exit ${rc}, want $2"; sed 's/^/    /' "$out"; fi
}
envval() { grep -E "^$2=" "$1" 2> /dev/null | tail -1 | cut -d= -f2- || true; }
profiles() { envval .env COMPOSE_PROFILES; }
has_profile() { case ",$(profiles)," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }
logged() { grep -qF -- "$1" "$STUB_LOG"; }

plan="$(python3 scripts/lib/addon_cli.py --addons-dir addons --data-root data plan-enable signal)"
case "$plan" in
  *'"sync":"scripts/sync-signal-profile.sh"'*) ok "plan-enable carries gateway.sync" ;;
  *) bad "plan-enable carries gateway.sync: $plan" ;;
esac
plan="$(python3 scripts/lib/addon_cli.py --addons-dir addons --data-root data plan-enable gptr)"
case "$plan" in
  *'"sync":""'*) ok "an add-on without a gateway plans an empty sync" ;;
  *) bad "an add-on without a gateway plans an empty sync: $plan" ;;
esac

# --- Signal ------------------------------------------------------------------
addon enable signal
expect_rc "enable signal without SIGNAL_ACCOUNT fails" 4
if [ "$(envval .env HERMES_SIGNAL_ENABLED)" = 0 ]; then ok "the failed enable rolls the flag back"; else bad "the failed enable rolls the flag back"; fi
if has_profile signal; then bad "the failed enable leaves signal out of COMPOSE_PROFILES"; else ok "the failed enable leaves signal out of COMPOSE_PROFILES"; fi
if logged "compose up"; then bad "the failed enable starts nothing"; else ok "the failed enable starts nothing"; fi

printf 'SIGNAL_ACCOUNT=+15550100001\nSIGNAL_ALLOWED_USERS=+15550100002\n' >> .env
: > "$STUB_LOG"
addon enable signal
expect_rc "enable signal" 0
if [ "$(envval .env HERMES_SIGNAL_ENABLED)" = 1 ]; then ok "enable sets HERMES_SIGNAL_ENABLED=1"; else bad "enable sets HERMES_SIGNAL_ENABLED=1"; fi
if has_profile signal; then ok "sync-profiles adds signal to COMPOSE_PROFILES"; else bad "sync-profiles adds signal to COMPOSE_PROFILES: $(profiles)"; fi
case ",$(envval .env COMPOSE_PROFILES_MANAGED)," in
  *,signal,*) ok "signal is a managed profile" ;;
  *) bad "signal is a managed profile" ;;
esac
for f in data/hermes/.env data/hermes/profiles/browser/.env; do
  if [ "$(envval "$f" SIGNAL_ACCOUNT)" = +15550100001 ] && [ "$(envval "$f" SIGNAL_HTTP_URL)" = http://signal-cli:8080 ] \
    && [ "$(envval "$f" SIGNAL_ALLOWED_USERS)" = +15550100002 ] && [ "$(envval "$f" SIGNAL_HOME_CHANNEL)" = +15550100001 ]; then
    ok "gateway.sync seeds the Signal variables in ${f}"
  else
    bad "gateway.sync seeds the Signal variables in ${f}"
  fi
done
if [ ! -e data/hermes/profiles/api-server/.env ]; then ok "api-server gets no Signal variables"; else bad "api-server gets no Signal variables"; fi
if grep -q 'nousresearch/hermes-agent:.* - browser signal' "$STUB_LOG"; then ok "the browser profile's Signal adapter is pinned off"; else bad "the browser profile's Signal adapter is pinned off"; cat "$STUB_LOG"; fi
if logged "compose up -d --wait signal-cli"; then ok "enable starts signal-cli"; else bad "enable starts signal-cli"; cat "$STUB_LOG"; fi
if logged "vault.sh reload-plugins"; then ok "enable reloads Hermes"; else bad "enable reloads Hermes"; fi
if logged "compose up -d --force-recreate hermes" || logged "compose restart hermes"; then bad "enable does not recreate hermes"; else ok "enable does not recreate hermes"; fi
if grep -qi "send" "$STUB_LOG"; then bad "nothing is sent"; else ok "nothing is sent"; fi
if [ -z "$(ls -A "${tmp}/synthetic-signal")" ]; then ok "the signal-cli data dir is left untouched"; else bad "the signal-cli data dir is left untouched"; fi

: > "$STUB_LOG"
addon disable signal
expect_rc "disable signal" 0
if [ "$(envval .env HERMES_SIGNAL_ENABLED)" = 0 ]; then ok "disable sets HERMES_SIGNAL_ENABLED=0"; else bad "disable sets HERMES_SIGNAL_ENABLED=0"; fi
if has_profile signal; then bad "disable removes signal from COMPOSE_PROFILES: $(profiles)"; else ok "disable removes signal from COMPOSE_PROFILES"; fi
for f in data/hermes/.env data/hermes/profiles/browser/.env; do
  if grep -q '^SIGNAL_' "$f"; then bad "gateway.sync clears the Signal variables in ${f}"; else ok "gateway.sync clears the Signal variables in ${f}"; fi
done
if logged "compose stop signal-cli"; then ok "disable stops signal-cli"; else bad "disable stops signal-cli"; cat "$STUB_LOG"; fi
if [ "$(envval .env SIGNAL_ACCOUNT)" = +15550100001 ]; then ok "disable keeps SIGNAL_ACCOUNT in .env"; else bad "disable keeps SIGNAL_ACCOUNT in .env"; fi

rmdir "${tmp}/synthetic-signal"
addon enable signal
expect_rc "enable signal with a missing SIGNAL_CLI_DATA_DIR fails" 4
if [ "$(envval .env HERMES_SIGNAL_ENABLED)" = 0 ] && ! has_profile signal; then ok "the rollback leaves Signal off"; else bad "the rollback leaves Signal off"; fi
if grep -q '^SIGNAL_' data/hermes/.env; then bad "the rollback leaves no Signal variables seeded"; else ok "the rollback leaves no Signal variables seeded"; fi

# --- Buzz --------------------------------------------------------------------
: > "$STUB_LOG"
addon enable buzz
expect_rc "enable buzz" 0
if [ "$(envval .env HERMES_BUZZ_ENABLED)" = 1 ]; then ok "enable sets HERMES_BUZZ_ENABLED=1"; else bad "enable sets HERMES_BUZZ_ENABLED=1"; fi
if has_profile buzz; then ok "sync-profiles adds buzz to COMPOSE_PROFILES"; else bad "sync-profiles adds buzz to COMPOSE_PROFILES: $(profiles)"; fi
f=data/hermes/profiles/engineer/.env
if [ "$(envval "$f" BUZZ_RELAY_URL)" = ws://localhost:3000 ] && [ "$(envval "$f" BUZZ_CLI_PATH)" = /opt/data/.local/bin/buzz ]; then
  ok "gateway.sync seeds the agent profile"
else
  bad "gateway.sync seeds the agent profile"
fi
if logged "compose up"; then bad "buzz starts no compose service"; cat "$STUB_LOG"; else ok "buzz starts no compose service"; fi
if logged "vault.sh reload-plugins"; then ok "enable buzz reloads Hermes"; else bad "enable buzz reloads Hermes"; fi

printf 'BUZZ_PRIVATE_KEY=nsec-synthetic\n' >> "$f"
: > "$STUB_LOG"
addon disable buzz
expect_rc "disable buzz" 0
if [ "$(envval .env HERMES_BUZZ_ENABLED)" = 0 ] && ! has_profile buzz; then ok "disable turns the flag and profile off"; else bad "disable turns the flag and profile off"; fi
if grep -q 'nousresearch/hermes-agent:.* - engineer$' "$STUB_LOG"; then ok "gateway.sync turns the agent profile's adapter off"; else bad "gateway.sync turns the agent profile's adapter off"; cat "$STUB_LOG"; fi
if grep -q '^BUZZ_PRIVATE_KEY=' "$f"; then ok "disable keeps the agent's key"; else bad "disable keeps the agent's key"; fi

# --- sync-profiles.sh --list ---------------------------------------------------
rows="$("$BASH_UNDER_TEST" scripts/sync-profiles.sh --list | grep -c '^HERMES_SIGNAL_ENABLED ' || true)"
if [ "$rows" = 1 ]; then ok "sync-profiles --list shows HERMES_SIGNAL_ENABLED once"; else bad "sync-profiles --list shows HERMES_SIGNAL_ENABLED once (got $rows)"; fi

if [ "$fails" -gt 0 ]; then
  echo "${fails} failed"
  exit 1
fi
echo "all gateway add-on checks passed"
