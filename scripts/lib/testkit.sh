# Pass/fail and bootstrap helpers for the shell tests in scripts/. POSIX sh,
# and safe under /bin/bash 3.2. Source it after `set -eu`:
#
#   . "${ROOT}/scripts/lib/testkit.sh"
#
# A failure prints `FAIL <message>` to stderr, as scripts/lib/testkit.py does.

# fail MESSAGE: report and exit 1.
fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

# fn_source FILE NAME: prints the definition of shell function NAME in FILE,
# from `NAME() {` at the start of a line to the first `}` line. Fails when
# FILE has no such function.
fn_source() {
  # The range is built in a variable: bash 3.2 (macOS /bin/sh) brace-expands
  # {/,/^} inside a quoted "$(sed ...)" that nests a second quoted string.
  _tk_range='/^'"$2"'() {/,/^}/p'
  _tk_src="$(sed -n "$_tk_range" "$1")"
  [ -n "$_tk_src" ] || fail "$2 missing from $1"
  printf '%s\n' "$_tk_src"
}

# load_fns FILE NAME...: defines each named function from FILE in this shell,
# so a test can call the real code (usually the bootstrap's). Fails when one
# is missing.
load_fns() {
  _tk_file="$1"
  shift
  for _tk_fn in "$@"; do
    # fn_source reports a missing function, but its exit only leaves the
    # $(...) subshell, so exit here too.
    _tk_def="$(fn_source "$_tk_file" "$_tk_fn")" || exit 1
    eval "$_tk_def"
    command -v "$_tk_fn" >/dev/null 2>&1 || fail "$_tk_fn missing from $_tk_file"
  done
}
