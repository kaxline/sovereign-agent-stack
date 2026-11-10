#!/bin/sh
# What 007-profile-ownership removes and chowns in a Hermes home.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
script="${ROOT}/compose/hermes/007-profile-ownership.sh"

# Short base: a Unix socket path is limited to about 104 bytes, and macOS's
# default TMPDIR is long.
tmp="$(mktemp -d /tmp/css.XXXXXX)"
trap 'rm -rf "$tmp"' EXIT

# sock <path>: bind a Unix socket at path and leave it behind.
sock() {
  mkdir -p "$(dirname "$1")"
  python3 -c 'import socket, sys; socket.socket(socket.AF_UNIX).bind(sys.argv[1])' "$1"
}

setup() {
  rm -rf "${tmp}/h"
  rt=home/.config/browser-harness/runtime
  sock "${tmp}/h/gateway.sock"
  sock "${tmp}/h/profiles/se/gateway.sock"
  sock "${tmp}/h/profiles/browser/${rt}/bu-default.sock"
  sock "${tmp}/h/${rt}/bu-job.sock"
  sock "${tmp}/h/profiles/browser/other.sock"
  printf 'x\n' > "${tmp}/h/profiles/browser/${rt}/notes.sock"
  printf '/tmp/hermes-gw-1/control.sock\n' > "${tmp}/h/gateway.sock.path"
}

# Stub chown: records its arguments, one per line. There is no hermes user
# here, so the chown pass runs only where a test names an owner.
mkdir -p "${tmp}/bin"
printf '#!/bin/sh\nprintf "%%s\\n" "$@" >> "%s/chowned"\n' "$tmp" > "${tmp}/bin/chown"
chmod +x "${tmp}/bin/chown"

# run [VAR=value...]: prints the script's output.
run() {
  : > "${tmp}/chowned"
  env -i PATH="${tmp}/bin:$PATH" HERMES_HOME="${tmp}/h" "$@" sh "$script"
}

setup
out="$(run HERMES_CONTROL_SOCKET_IN_TMP=1 BH_RUNTIME_DIR=/tmp/browser-harness)"
for gone in gateway.sock profiles/se/gateway.sock \
  profiles/browser/home/.config/browser-harness/runtime/bu-default.sock \
  home/.config/browser-harness/runtime/bu-job.sock; do
  [ ! -e "${tmp}/h/${gone}" ] || fail "${gone} was not removed"
done
[ -S "${tmp}/h/profiles/browser/other.sock" ] || fail "a socket at another path was removed"
[ -f "${tmp}/h/profiles/browser/home/.config/browser-harness/runtime/notes.sock" ] || fail "a regular file was removed"
[ -f "${tmp}/h/gateway.sock.path" ] || fail "the control socket pointer was removed"
[ "$out" = "[007-profile-ownership] removed 4 stale socket(s)" ] || fail "unexpected log: $out"

# Each path is cleared only while its socket lives on /tmp.
setup
out="$(run)"
[ -z "$out" ] || fail "nothing should be removed without the env: $out"
[ -S "${tmp}/h/profiles/se/gateway.sock" ] || fail "control socket removed without HERMES_CONTROL_SOCKET_IN_TMP=1"
[ -S "${tmp}/h/${rt}/bu-job.sock" ] || fail "browser-harness socket removed without BH_RUNTIME_DIR"
out="$(run BH_RUNTIME_DIR="${tmp}/h/bh")"
[ -S "${tmp}/h/${rt}/bu-job.sock" ] || fail "browser-harness socket removed while BH_RUNTIME_DIR is in the home"

# Chown pass: an owner nothing here has, so every file and directory under
# profiles/ is foreign. Sockets are left to the removal above.
setup
out="$(run PROFILE_OWNER_UID=99999 PROFILE_OWNER_GID=99999)"
grep -qx '99999:99999' "${tmp}/chowned" || fail "chown not called with the owner"
grep -q "/profiles/browser/${rt}/notes.sock$" "${tmp}/chowned" || fail "a foreign regular file was not chowned"
grep -qx "${tmp}/h/profiles/se" "${tmp}/chowned" || fail "a foreign directory was not chowned"
if grep -q 'other.sock$' "${tmp}/chowned"; then fail "a socket was passed to chown"; fi
if grep -qx "${tmp}/h/gateway.sock.path" "${tmp}/chowned"; then fail "only profiles/ is chowned"; fi
case "$out" in
  *"chowned "*" file(s) under profiles/ to hermes"*) ;;
  *) fail "no chown log line: $out" ;;
esac

# Already owned: nothing to chown, nothing logged. The owner is read from the
# tree, since macOS gives files under /tmp the folder's group.
owner="$(python3 -c 'import os, sys; s = os.stat(sys.argv[1]); print(s.st_uid, s.st_gid)' "${tmp}/h/profiles")"
out="$(run PROFILE_OWNER_UID="${owner% *}" PROFILE_OWNER_GID="${owner#* }")"
[ ! -s "${tmp}/chowned" ] || fail "chowned files that were already owned: $(tr '\n' ' ' < "${tmp}/chowned")"
[ -z "$out" ] || fail "unexpected log with nothing to do: $out"

printf 'OK profile ownership\n'
