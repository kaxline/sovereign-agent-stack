#!/usr/bin/env bash
# scripts/lib/secret.sh: random_value's formats, and ensure_secret_file's
# modes, atomic replace, kept files, a directory at the path, warn vs die, and
# no openssl needed. Also keeps the stack scripts from growing their own
# generators again (AS-0015).
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"

tmp="$(mktemp -d)"
trap 'chmod -R u+w "$tmp" 2> /dev/null; rm -rf "$tmp"' EXIT
mode_of() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }

# A PATH without openssl: the tools the lib needs, linked into one folder.
nobin="${tmp}/bin"
mkdir -p "$nobin"
for t in od tr head base64 mktemp chmod mv rm mkdir dirname basename cat; do
  ln -s "$(command -v "$t")" "${nobin}/${t}"
done
lib="${ROOT}/scripts/lib/secret.sh"
# in_lib CODE: runs CODE in bash with the lib sourced and openssl off PATH.
in_lib() {
  # shellcheck disable=SC2016 # expanded by the inner bash
  PATH="$nobin" "$BASH" -c 'set -euo pipefail; . "$1"; shift; eval "$1"' _ "$lib" "$1"
}
in_lib 'command -v openssl' > /dev/null 2>&1 && fail "openssl is still on the test PATH"

# --- random_value --------------------------------------------------------------
v="$(in_lib 'random_value hex 32')" || fail "random_value hex 32 fails without openssl"
printf '%s' "$v" | grep -qE '^[0-9a-f]{64}$' || fail "random_value hex 32: want 64 hex characters, got [$v]"
v="$(in_lib 'random_value hex 24')"
[ "${#v}" = 48 ] || fail "random_value hex 24: want 48 characters, got ${#v}"
# Long enough for od to repeat a line, which -v keeps from becoming '*'.
v="$(in_lib 'random_value hex 4096')"
[ "${#v}" = 8192 ] || fail "random_value hex 4096 is 8192 characters"
if printf '%s' "$v" | grep -q '[^0-9a-f]'; then fail "random_value hex 4096 is all hex"; fi
v="$(in_lib 'random_value base64 32')"
printf '%s' "$v" | grep -qE '^[A-Za-z0-9+/]{43}=$' || fail "random_value base64 32: want openssl's 44-character form, got [$v]"
[ "$(in_lib 'random_value hex 16')" != "$(in_lib 'random_value hex 16')" ] || fail "two values differ"
in_lib 'random_value rot13 8' > /dev/null 2>&1 && fail "an unknown encoding fails"

# --- ensure_secret_file ----------------------------------------------------------
d="${tmp}/a/secrets"
f="${d}/token"
in_lib "ensure_secret_file '$f'; echo \"created=\$SECRET_FILE_CREATED\"" > "${tmp}/out" || fail "ensure_secret_file on a fresh path"
grep -qx 'created=1' "${tmp}/out" || fail "SECRET_FILE_CREATED=1 after writing"
[ "$(mode_of "$d")" = 700 ] || fail "the folder is 0700"
[ "$(mode_of "$f")" = 600 ] || fail "the file is 0600"
[ "$(wc -c < "$f" | tr -d ' ')" = 65 ] || fail "newline (default): 64 hex characters and a newline"
grep -qxE '[0-9a-f]{64}' "$f" || fail "the file holds 32 bytes as hex"
[ -z "$(find "$d" -name '.token.*')" ] || fail "no temp file left behind"

# A non-empty file is kept, and its mode fixed.
first="$(cat "$f")"
chmod 644 "$f"
in_lib "ensure_secret_file '$f'; echo \"created=\$SECRET_FILE_CREATED\"" > "${tmp}/out" || fail "ensure_secret_file again"
grep -qx 'created=0' "${tmp}/out" || fail "SECRET_FILE_CREATED=0 when kept"
[ "$(cat "$f")" = "$first" ] || fail "keeps an existing token"
[ "$(mode_of "$f")" = 600 ] || fail "a kept file is set to 0600"

# An empty file is filled; an existing folder is set to 0700.
: > "$f"
chmod 755 "$d"
in_lib "ensure_secret_file '$f'" || fail "ensure_secret_file on an empty file"
[ -s "$f" ] || fail "fills an empty file"
[ "$(mode_of "$d")" = 700 ] || fail "an existing folder is set to 0700"

# bare: 64 characters, no newline.
b="${tmp}/b/token"
in_lib "ensure_secret_file '$b' die bare" || fail "ensure_secret_file bare"
[ "$(wc -c < "$b" | tr -d ' ')" = 64 ] || fail "bare: 64 characters, no newline"

# write_secret_file replaces through a rename: a reader holding the old file
# still sees the old token whole, and a hard link to it is left alone.
ln "$b" "${tmp}/b-link"
old="$(cat "$b")"
in_lib "write_secret_file '$b' die bare" || fail "write_secret_file"
[ "$(cat "$b")" != "$old" ] || fail "write_secret_file replaces the token"
[ "$(cat "${tmp}/b-link")" = "$old" ] || fail "the old inode is untouched (temp then rename)"
[ "$(mode_of "$b")" = 600 ] || fail "a replaced file is 0600"

# A directory at the path (Docker's missing bind source): warn returns 1 and
# says so through the script's warn; die exits 1 through the script's die.
x="${tmp}/c/token"
mkdir -p "$x"
set +e
out="$(in_lib "warn() { printf 'W:%s\n' \"\$1\"; }; ensure_secret_file '$x' warn || echo rc=\$?; echo after" 2>&1)"
set -e
printf '%s\n' "$out" | grep -q "^W:${x} is a directory, so no token was written" || fail "warn mode uses the script's warn: $out"
printf '%s\n' "$out" | grep -qx 'rc=1' || fail "warn mode returns 1"
printf '%s\n' "$out" | grep -qx 'after' || fail "warn mode carries on"
set +e
out="$(in_lib "die() { printf 'D:%s\n' \"\$1\" >&2; exit 7; }; ensure_secret_file '$x' die; echo after" 2>&1)"
rc=$?
set -e
[ "$rc" = 7 ] || fail "die mode calls the script's die (rc $rc)"
printf '%s\n' "$out" | grep -q "^D:${x} is a directory" || fail "die mode passes the message to die: $out"
printf '%s\n' "$out" | grep -qx 'after' && fail "die mode stops"
# Without the script's own functions: plain warning: / error: lines.
set +e
out="$(in_lib "ensure_secret_file '$x' warn || echo rc=\$?" 2>&1)"
set -e
printf '%s\n' "$out" | grep -q "^warning: ${x} is a directory" || fail "default warn line: $out"
set +e
out="$(in_lib "ensure_secret_file '$x'" 2>&1)"
rc=$?
set -e
[ "$rc" = 1 ] || fail "die is the default mode and exits 1"
printf '%s\n' "$out" | grep -q "^error: ${x} is a directory" || fail "default error line: $out"
[ -d "$x" ] && [ -z "$(ls -A "$x")" ] || fail "the directory is left as it was"

# A file where the folder should be.
: > "${tmp}/notdir"
set +e
out="$(in_lib "ensure_secret_file '${tmp}/notdir/token' warn" 2>&1)"
set -e
printf '%s\n' "$out" | grep -q "notdir exists and is not a folder" || fail "a file at the folder path is reported: $out"

# A folder it can't write: the message carries SECRET_FILE_FIX.
if [ "$(id -u)" != 0 ]; then
  ro="${tmp}/ro"
  mkdir -p "$ro"
  chmod 500 "$ro"
  set +e
  out="$(in_lib "SECRET_FILE_FIX='run once: fix-it'; ensure_secret_file '${ro}/sub/token' warn" 2>&1)"
  set -e
  printf '%s\n' "$out" | grep -q "could not create ${ro}/sub at 0700; run once: fix-it" \
    || fail "an unwritable parent is reported with the fix: $out"
  chmod 700 "$ro"
  mkdir -p "${ro}/sub"
  chmod 500 "${ro}/sub"
  set +e
  out="$(in_lib "chmod() { [ \"\$1\" != 700 ] && command chmod \"\$@\"; return 0; }; SECRET_FILE_FIX='run once: fix-it'; ensure_secret_file '${ro}/sub/token' warn" 2>&1)"
  set -e
  printf '%s\n' "$out" | grep -q "could not write ${ro}/sub/token; run once: fix-it" \
    || fail "an unwritable folder is reported with the fix: $out"
  chmod 700 "${ro}/sub"
fi

# --- no generators outside the lib -----------------------------------------------
for s in scripts/*.sh scripts/lib/*.sh; do
  case "$s" in */test-*|scripts/lib/secret.sh) continue ;; esac
  # doctor.sh names the command in a hint for the user.
  if grep 'openssl rand' "${ROOT}/${s}" | grep -vq 'use hex (openssl rand -hex 24)'; then
    fail "${s} calls openssl rand; use random_value from lib/secret.sh"
  fi
  if grep -q '/dev/urandom' "${ROOT}/${s}"; then fail "${s} reads /dev/urandom; use lib/secret.sh"; fi
  if grep -q '^ *random_value() {' "${ROOT}/${s}"; then fail "${s} defines its own random_value"; fi
done
for s in addon detector web-browser-secrets vault setup budget crm db todo; do
  # shellcheck disable=SC2016 # a literal line
  grep -q '^source "${ROOT}/scripts/lib/secret.sh"$' "${ROOT}/scripts/${s}.sh" \
    || fail "scripts/${s}.sh sources lib/secret.sh"
done
grep -q 'require_cmd openssl' "${ROOT}/scripts/setup.sh" && fail "setup.sh no longer needs openssl"

echo "OK lib/secret.sh"
