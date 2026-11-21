#!/bin/sh
# scripts/addon.sh enable bitwarden --json against the real addons/bitwarden
# manifest and scripts/vault.sh, in a scratch ROOT with host python3 and a
# stub docker (CR 0035). With no add-on store and BITWARDEN_ENABLED off,
# enable's prepare hook (vault.sh prepare) creates every folder the broker
# bind-mounts before docker compose up: instances/ in the store and bw/,
# broker/, broker-api/ (with api-token) and secrets/ under
# BITWARDEN_HOST_DATA_DIR, secrets/ at 0711 and the others at 0700. A second
# enable keeps the token and the modes, and a folder that can't be created
# fails enable at step prepare with the flag still off.
#
#   sh scripts/test-bitwarden-enable-dirs.sh
#
# KEEP_TMP=1 keeps the scratch directory and prints its path.
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BASH_UNDER_TEST=bash
[ -x /bin/bash ] && BASH_UNDER_TEST=/bin/bash

# The physical path: addon.sh resolves the data root through symlinks
# (/var -> /private/var on macOS).
tmp="$(cd "$(mktemp -d)" && pwd -P)"
trap 'chmod -R u+rwx "$tmp" 2> /dev/null; [ -n "${KEEP_TMP:-}" ] && echo "kept $tmp" || rm -rf "$tmp"' EXIT
root="${tmp}/stack"
data="${tmp}/dataroot"
bw="${tmp}/bwdata"
mkdir -p "${root}/scripts/lib" "${tmp}/bin" "${tmp}/out"
for f in addon.sh sync-profiles.sh vault.sh; do
  cp "${REPO}/scripts/${f}" "${root}/scripts/"
done
for f in env.sh data-root.sh secret.sh python.sh addon-events.sh stack-lock.sh addon_cli.py addon_manifest.py addon_store.py; do
  cp "${REPO}/scripts/lib/${f}" "${root}/scripts/lib/"
done
cp -R "${REPO}/addons" "${root}/addons"
cp "${REPO}/docker-compose.yml" "${root}/"
# The env readers take a key's first line, so these replace the example's
# values rather than being appended after them.
grep -v -e '^ASSISTANT_DATA_ROOT=' -e '^BITWARDEN_ENABLED=' -e '^HERMES_UID=' -e '^HERMES_GID=' \
  "${REPO}/.env.example" > "${root}/.env"
{
  printf 'COMPOSE_PROJECT_NAME=bwdirstest\n'
  printf 'ASSISTANT_DATA_ROOT=%s\n' "$data"
  printf 'BITWARDEN_HOST_DATA_DIR=%s\n' "$bw"
  printf 'HERMES_UID=%s\nHERMES_GID=%s\n' "$(id -u)" "$(id -g)"
} >> "${root}/.env"

# docker logs its argv. At `up` it records each broker bind source and its
# mode, or "missing", so the check sees the folders as compose would.
cat > "${tmp}/bin/docker" <<'EOF'
#!/bin/sh
printf 'docker %s\n' "$*" >> "${STUB_LOG}"
case " $* " in
  *" up "*)
    for d in "${STUB_DATA}/addons/bitwarden/instances" "${STUB_BW}/secrets" "${STUB_BW}/bw" \
      "${STUB_BW}/broker" "${STUB_BW}/broker-api"; do
      if [ -d "$d" ]; then
        m="$(stat -c %a "$d" 2> /dev/null || stat -f %Lp "$d")"
        printf 'at up: %s %s\n' "${d##*/}" "$m" >> "${STUB_LOG}"
      else
        printf 'at up: %s missing\n' "${d##*/}" >> "${STUB_LOG}"
      fi
    done
    [ -s "${STUB_BW}/broker-api/api-token" ] && printf 'at up: api-token\n' >> "${STUB_LOG}"
    ;;
esac
exit 0
EOF
cat > "${tmp}/bin/make" <<'EOF'
#!/bin/sh
printf 'make %s\n' "$*" >> "${STUB_LOG}"
exit 0
EOF
chmod +x "${tmp}/bin/docker" "${tmp}/bin/make"
mkdir -p "${tmp}/hosttmp"
export TMPDIR="${tmp}/hosttmp"
export PATH="${tmp}/bin:${PATH}"
export STACK_PYTHON=host
export STUB_LOG="${tmp}/stub.log"
export STUB_DATA="$data" STUB_BW="$bw"
: > "$STUB_LOG"
unset ASSISTANT_DATA_ROOT ADDONS_DIR BITWARDEN_HOST_DATA_DIR BITWARDEN_ENABLED COMPOSE_PROJECT_NAME ENV_FILE 2> /dev/null || true

repo_before="$(git -C "$REPO" status --porcelain 2> /dev/null || true)"

fail=0
n=0
ok() { echo "ok: $1"; }
bad() { echo "FAIL: $1"; fail=1; }
addon() {
  n=$((n + 1))
  out="${tmp}/out/${n}"
  err="${tmp}/out/${n}.err"
  rc=0
  "$BASH_UNDER_TEST" "${root}/scripts/addon.sh" "$@" > "$out" 2> "$err" || rc=$?
}
json_ok() {
  python3 -c 'import json, sys
lines = [json.loads(l) for l in open(sys.argv[1]).read().splitlines()]
assert lines and all(isinstance(l, dict) for l in lines)
last = lines[-1]
steps = [(l["step"], l.get("status")) for l in lines if l.get("phase") == "setup"]
assert last["phase"] == sys.argv[2]
sys.exit(0 if eval(sys.argv[3]) else 1)' "$out" "$1" "${2:-True}" 2> /dev/null
}
show() { sed 's/^/    /' "$out"; sed 's/^/    err: /' "$err"; }
envval() { sed -n "s/^$1=//p" "${root}/.env" | tail -1; }
mode_of() { stat -c %a "$1" 2> /dev/null || stat -f %Lp "$1"; }

# --- First enable on a store-less install -----------------------------------
addon enable bitwarden --json
if [ "$rc" = 0 ] && json_ok "done" 'steps.index(("prepare","done")) < steps.index(("flags","running")) and last["id"] == "bitwarden"'; then
  ok "enable bitwarden --json ends with done, prepare before flags"
else
  bad "enable bitwarden --json ends with done, prepare before flags (exit $rc)"; show
fi
if grep -q '^docker compose .*up ' "$STUB_LOG"; then ok "enable runs docker compose up"; else bad "enable runs docker compose up"; cat "$STUB_LOG"; fi
want="at up: instances 700
at up: secrets 711
at up: bw 700
at up: broker 700
at up: broker-api 700
at up: api-token"
if [ "$(grep '^at up:' "$STUB_LOG")" = "$want" ]; then
  ok "every broker bind source exists at up, secrets 0711 and the rest 0700"
else
  bad "every broker bind source exists at up, secrets 0711 and the rest 0700"; grep '^at up:' "$STUB_LOG" | sed 's/^/    /'
fi
if [ "$(envval BITWARDEN_ENABLED)" = 1 ]; then ok "the flag is on after enable"; else bad "the flag is on after enable"; fi

# --- A second enable changes nothing ----------------------------------------
token="$(cat "${bw}/broker-api/api-token")"
chmod 711 "${bw}/secrets"
addon enable bitwarden --json
if [ "$rc" = 0 ] && json_ok "done"; then ok "a second enable ends with done"; else bad "a second enable ends with done (exit $rc)"; show; fi
if [ "$(cat "${bw}/broker-api/api-token")" = "$token" ] && [ "$(mode_of "${bw}/secrets")" = 711 ] \
  && [ "$(mode_of "${data}/addons/bitwarden/instances")" = 700 ]; then
  ok "a second enable keeps the token and the modes"
else
  bad "a second enable keeps the token and the modes"
fi

# --- A folder that can't be created fails prepare ----------------------------
addon disable bitwarden --json
rm -rf "$bw"
: > "$bw"
: > "$STUB_LOG"
addon enable bitwarden --json
if [ "$rc" != 0 ] && json_ok error 'last["step"] == "prepare" and ("flags","running") not in steps'; then
  ok "an uncreatable data dir fails enable at step prepare"
else
  bad "an uncreatable data dir fails enable at step prepare (exit $rc)"; show
fi
if [ "$(envval BITWARDEN_ENABLED)" != 1 ] && ! grep -q ' up ' "$STUB_LOG"; then
  ok "a failed prepare leaves the flag off and starts nothing"
else
  bad "a failed prepare leaves the flag off and starts nothing"; cat "$STUB_LOG"
fi

if [ "$(git -C "$REPO" status --porcelain 2> /dev/null || true)" = "$repo_before" ]; then
  ok "the repo is untouched"
else
  bad "the repo is untouched"
fi

if [ "$fail" = 0 ]; then echo "PASS"; else echo "FAILED"; exit 1; fi
