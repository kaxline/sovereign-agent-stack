# Host python3 check for stack scripts.
# Source from repo-root scripts:  source "${ROOT}/scripts/lib/python.sh"

# Fails unless a working python3 >= 3.9 is on PATH. Runs the interpreter
# instead of trusting command -v: on a Mac without the Command Line Tools,
# /usr/bin/python3 is a stub that opens an install dialog and exits non-zero.
require_python3() {
  local hint="Install Python 3.9+ (macOS: xcode-select --install, or brew install python)"
  if ! command -v python3 >/dev/null 2>&1; then
    printf 'error: python3 not found. %s\n' "$hint" >&2
    exit 1
  fi
  if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    printf 'error: python3 is missing, a stub, or older than 3.9 (%s). %s\n' \
      "$(command -v python3)" "$hint" >&2
    exit 1
  fi
}
