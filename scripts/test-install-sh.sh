#!/usr/bin/env bash
# scripts/install.sh and scripts/release-tarball.sh on synthetic releases:
# fresh install, the same version again, an upgrade that keeps .env, data and
# local config, a failed upgrade rolling back, and the refusals. The real
# backup.sh and upgrade.sh run (BACKUP_TAR=host); setup.sh, up.sh and
# doctor.sh are stubs that log, and docker is fake.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
CALLS="${tmp}/calls"
FAKE="${tmp}/fake"
mkdir -p "${tmp}/bin" "$FAKE" "${tmp}/out"
export CALLS FAKE PATH="${tmp}/bin:${PATH}" BACKUP_TAR=host

cat > "${tmp}/bin/docker" << 'EOF'
#!/bin/sh
case "$1 $2" in
  "--version ") [ -f "${FAKE}/podman" ] && echo "podman version 5" || echo "Docker version 27"; exit 0 ;;
  "info "*|"compose version") exit 0 ;;
  "compose stop") echo "docker compose stop" >> "$CALLS" ;;
  "compose config") printf '{\n  "name": "inst",\n  "services": {}\n}\n' ;;
  "compose ps"|"volume ls") exit 0 ;;
  *) echo "unexpected docker $*" >&2; exit 1 ;;
esac
EOF
chmod +x "${tmp}/bin/docker"

# release VERSION CODE [HOOK] [UP_FAILS]: a synthetic release source.
release() {
  src="${tmp}/src-$1"
  mkdir -p "${src}/scripts/lib" "${src}/upgrades"
  cp "${ROOT}/scripts/backup.sh" "${ROOT}/scripts/upgrade.sh" "${src}/scripts/"
  cp "${ROOT}/scripts/lib/env.sh" "${ROOT}/scripts/lib/data-root.sh" "${ROOT}/scripts/lib/python.sh" "${ROOT}/scripts/lib/json-lines.sh" \
    "${src}/scripts/lib/"
  cp "${ROOT}/upgrades/lib.sh" "${src}/upgrades/"
  printf '%s\n' "$2" > "${src}/code.txt"
  # shellcheck disable=SC2016  # expanded by the stubs
  {
    printf '#!/bin/sh\necho "setup $*" >> "$CALLS"\n[ -f .env ] || echo "SETUP=1" > .env\nmkdir -p data/hermes\n[ -f data/hermes/state.txt ] || echo fresh > data/hermes/state.txt\n' > "${src}/scripts/setup.sh"
    printf '#!/bin/sh\necho "up %s $*" >> "$CALLS"\nexit %s\n' "$1" "${4:-0}" > "${src}/scripts/up.sh"
    printf '#!/bin/sh\necho "doctor" >> "$CALLS"\n[ ! -f "${FAKE}/doctor-fails" ]\n' > "${src}/scripts/doctor.sh"
  }
  # shellcheck disable=SC2016  # expanded by the hook
  [ -z "${3:-}" ] || printf '#!/usr/bin/env bash\nsource "$(dirname "$0")/lib.sh"\n%s\n' "$3" > "${src}/upgrades/$1.sh"
  bash "${ROOT}/scripts/release-tarball.sh" "$1" "${tmp}/out" --src "$src" > /dev/null
}

DIR="${tmp}/host/stack"
inst() { bash "${ROOT}/scripts/install.sh" --dir "$DIR" --json "$@"; }
tarball() { printf '%s/out/sovereign-agent-stack-%s.tar.gz' "$tmp" "$1"; }
last() { printf '%s\n' "$1" | tail -1; }
jget() { python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(eval(sys.argv[2]))' "$(last "$1")" "$2"; }

expect_error() {
  want_rc="$1" want="$2"
  shift 2
  set +e
  out="$(inst "$@" 2> /dev/null)"
  rc=$?
  set -e
  [ "$rc" = "$want_rc" ] || fail "$* exit $rc, want $want_rc: $out"
  case "$(last "$out")" in *"\"error\":\"${want}\""*) ;; *) fail "$*: want ${want}, got $out" ;; esac
}

release v0.0.1 one
printf 'gone\n' > "${tmp}/src-v0.0.1/gone.txt"
bash "${ROOT}/scripts/release-tarball.sh" v0.0.1 "${tmp}/out" --src "${tmp}/src-v0.0.1" > /dev/null
tar -tzf "$(tarball v0.0.1)" | grep -qx 'sovereign-agent-stack-v0.0.1/.stack-files' || fail "tarball lacks .stack-files"
release v0.0.2 two 'needs_user two-id "do the two thing"; notice "two note"'
release v0.0.3 three '' 1

# --- refusals before anything is written ----------------------------------------
expect_error 2 bad_input --version 1.0
expect_error 2 bad_input --version v0.0.1 --tarball "$(tarball v0.0.1)" --sha256 x --dir ""
mkdir -p "${tmp}/gitdir/.git"
set +e
out="$(bash "${ROOT}/scripts/install.sh" --version v0.0.1 --dir "${tmp}/gitdir" --tarball "$(tarball v0.0.1)" --json 2> /dev/null)"
set -e
case "$out" in *'git checkout'*) ;; *) fail "git checkout dir: $out" ;; esac
mkdir -p "${tmp}/full" && touch "${tmp}/full/x"
set +e
out="$(bash "${ROOT}/scripts/install.sh" --version v0.0.1 --dir "${tmp}/full" --tarball "$(tarball v0.0.1)" --json 2> /dev/null)"
set -e
case "$out" in *'not empty'*) ;; *) fail "non-empty dir: $out" ;; esac
expect_error 1 checksum_mismatch --version v0.0.1 --tarball "$(tarball v0.0.1)" --sha256 deadbeef
touch "${FAKE}/podman"
expect_error 1 unsupported_host --version v0.0.1 --tarball "$(tarball v0.0.1)"
rm "${FAKE}/podman"
expect_error 2 bad_input --version v0.0.2 --tarball "$(tarball v0.0.1)" --sha256 "$(cut -d' ' -f1 < "$(tarball v0.0.1).sha256")"
[ ! -e "${DIR}/code.txt" ] || fail "a refused install wrote files"

# --- fresh install, downloaded over file:// -----------------------------------------
mkdir -p "${tmp}/rel/v0.0.1"
cp "$(tarball v0.0.1)" "$(tarball v0.0.1).sha256" "${tmp}/rel/v0.0.1/"
: > "$CALLS"
out="$(STACK_RELEASE_BASE="file://${tmp}/rel" inst --version v0.0.1 -- --coding)" || fail "fresh install: $out"
[ "$(jget "$out" 'd["fresh"], d["previous_version"], d["doctor"]')" = "(True, None, 'passed')" ] || fail "fresh done: $out"
{ [ "$(cat "${DIR}/code.txt")" = one ] && [ -f "${DIR}/gone.txt" ]; } || fail "fresh files"
[ "$(cat "$CALLS")" = "$(printf 'setup --non-interactive --coding\nup v0.0.1 \ndoctor')" ] || fail "fresh calls: $(cat "$CALLS")"
[ -z "$(find "$DIR" -maxdepth 1 -name '.install*')" ] || fail "install left its temp dir or lock"

# Same version again: no backup, setup, upgrade start, doctor.
: > "$CALLS"
out="$(inst --version v0.0.1 --tarball "$(tarball v0.0.1)")" || fail "rerun: $out"
[ "$(jget "$out" 'd["fresh"], d["previous_version"], d["backup"]')" = "(False, 'v0.0.1', None)" ] || fail "rerun done: $out"
grep -q '^up v0.0.1' "$CALLS" || fail "rerun did not start"

# --- upgrade ---------------------------------------------------------------------
printf 'SETUP=1\nMINE=keep\n' > "${DIR}/.env"
printf 'override\n' > "${DIR}/docker-compose.override.yml"
printf 'chats\n' > "${DIR}/data/hermes/state.txt"
: > "$CALLS"
out="$(inst --version v0.0.2 --tarball "$(tarball v0.0.2)")" || fail "upgrade: $out"
[ "$(cat "${DIR}/code.txt")" = two ] || fail "upgrade code"
[ ! -e "${DIR}/gone.txt" ] || fail "a file v0.0.2 dropped is still there"
grep -q '^MINE=keep$' "${DIR}/.env" || fail ".env lost"
[ -f "${DIR}/docker-compose.override.yml" ] || fail "override lost"
[ "$(cat "${DIR}/data/hermes/state.txt")" = chats ] || fail "data lost"
[ "$(head -1 "${DIR}/.stack-version")" = v0.0.2 ] || fail "stamp"
[ "$(jget "$out" 'd["previous_version"], [n["id"] for n in d["needs_user"]], [n["message"] for n in d["notices"]]')" \
  = "('v0.0.1', ['two-id'], ['two note'])" ] || fail "upgrade done: $out"
backup="$(jget "$out" 'd["backup"]')"
{ [ -f "${backup}/stack.tar.gz" ] && [ -f "${backup}/data.tar.gz" ]; } || fail "no backup at ${backup}"
tar -tzf "${backup}/stack.tar.gz" | grep -q 'install-tmp' && fail "backup holds the install temp dir"
head -1 "$CALLS" | grep -qx 'docker compose stop' || fail "upgrade did not stop first: $(cat "$CALLS")"

expect_error 2 bad_input --version v0.0.1 --tarball "$(tarball v0.0.1)"

# --- a failed upgrade rolls back the files and starts the old release -------------
: > "$CALLS"
expect_error 4 command_failed --version v0.0.3 --tarball "$(tarball v0.0.3)"
[ "$(jget "$out" 'd["rolledBack"], d["step"]')" = "(True, 'upgrade')" ] || fail "rollback error: $out"
case "$out" in *'backup.sh restore --from'*) ;; *) fail "rollback message names no backup: $out" ;; esac
[ "$(cat "${DIR}/code.txt")" = two ] || fail "files not rolled back"
[ "$(head -1 "${DIR}/.stack-version")" = v0.0.2 ] || fail "stamp not rolled back"
grep -q '^MINE=keep$' "${DIR}/.env" || fail ".env lost in rollback"
tail -1 "$CALLS" | grep -q '^up v0.0.2' || fail "old release not started: $(cat "$CALLS")"

# doctor_failed leaves the stack running and says so.
touch "${FAKE}/doctor-fails"
expect_error 1 doctor_failed --version v0.0.2 --tarball "$(tarball v0.0.2)"
rm "${FAKE}/doctor-fails"

echo "ok install.sh"
