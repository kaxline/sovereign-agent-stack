#!/bin/sh
# Which sockets 007-clear-stale-sockets removes from a Hermes home.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
script="${ROOT}/compose/hermes/007-clear-stale-sockets.sh"

# Short base: a Unix socket path is limited to about 104 bytes, and macOS's
# default TMPDIR is long.
tmp="$(mktemp -d /tmp/css.XXXXXX)"
trap 'rm -rf "$tmp"' EXIT

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

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

# run [VAR=value...]: prints the script's output.
run() {
  env -i PATH="$PATH" HERMES_HOME="${tmp}/h" "$@" sh "$script"
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
[ "$out" = "[007-clear-stale-sockets] removed 4 stale socket(s)" ] || fail "unexpected log: $out"

# Each path is cleared only while its socket lives on /tmp.
setup
out="$(run)"
[ -z "$out" ] || fail "nothing should be removed without the env: $out"
[ -S "${tmp}/h/profiles/se/gateway.sock" ] || fail "control socket removed without HERMES_CONTROL_SOCKET_IN_TMP=1"
[ -S "${tmp}/h/${rt}/bu-job.sock" ] || fail "browser-harness socket removed without BH_RUNTIME_DIR"
out="$(run BH_RUNTIME_DIR="${tmp}/h/bh")"
[ -S "${tmp}/h/${rt}/bu-job.sock" ] || fail "browser-harness socket removed while BH_RUNTIME_DIR is in the home"

printf 'OK clear stale sockets\n'
