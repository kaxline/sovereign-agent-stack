#!/bin/sh
# shellcheck disable=SC2015  # "A && B || fail" is the check: fail runs when either test is false
# scripts/lib/testkit.sh and testkit.py: the helpers every other test uses.
#
#   sh scripts/test-testkit.sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# The checks below need their own reporting, since they test `fail` itself.
bad() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

cat > "${tmp}/src.sh" <<'EOF'
greet() {
  printf 'hi %s\n' "$1"
}

other() { :; }
EOF

# run SHELL SCRIPT: runs SCRIPT with testkit.sh sourced; sets out and rc.
run() {
  rc=0
  out="$("$1" -c ". '${ROOT}/scripts/lib/testkit.sh'; $2" 2>&1)" || rc=$?
}

for sh in sh bash; do
  command -v "$sh" >/dev/null 2>&1 || continue

  run "$sh" "fail 'it broke'; echo after"
  [ "$rc" = 1 ] && [ "$out" = "FAIL it broke" ] || bad "$sh: fail prints FAIL and exits 1 (rc=$rc, out=$out)"

  run "$sh" "load_fns '${tmp}/src.sh' greet; greet you"
  [ "$rc" = 0 ] && [ "$out" = "hi you" ] || bad "$sh: load_fns defines the function (rc=$rc, out=$out)"

  run "$sh" "load_fns '${tmp}/src.sh' greet nope; echo after"
  [ "$rc" = 1 ] && [ "$out" = "FAIL nope missing from ${tmp}/src.sh" ] \
    || bad "$sh: load_fns fails on a missing function (rc=$rc, out=$out)"

  run "$sh" "fn_source '${tmp}/src.sh' greet"
  [ "$rc" = 0 ] && [ "$(printf '%s\n' "$out" | head -1)" = "greet() {" ] && [ "$(printf '%s\n' "$out" | wc -l | tr -d ' ')" = 3 ] \
    || bad "$sh: fn_source prints the function (rc=$rc, out=$out)"
done

# testkit.py: run CODE; sets out and rc.
py() {
  rc=0
  out="$(python3 -c "import sys; sys.path.insert(0, '${ROOT}/scripts/lib'); import testkit as tk; $1" 2>&1)" || rc=$?
}

py "tk.expect(True, 'x'); print('after')"
[ "$rc" = 0 ] && [ "$out" = after ] || bad "py: expect passes a true condition (rc=$rc, out=$out)"
py "tk.expect(0, 'zero'); print('after')"
[ "$rc" = 1 ] && [ "$out" = "FAIL zero" ] || bad "py: expect fails a false condition and stops (rc=$rc, out=$out)"
py "import io; sys.stderr = io.StringIO(); tk.fail('loud')"
[ "$rc" = 1 ] && [ "$out" = "FAIL loud" ] || bad "py: fail reaches the terminal while stderr is captured (rc=$rc, out=$out)"
py "c = tk.Checks(); c.expect(1, 'a'); c.finish('all good'); print('next part')"
[ "$rc" = 0 ] && [ "$out" = "all good
next part" ] || bad "py: Checks.finish passes and returns with no failures (rc=$rc, out=$out)"
py "c = tk.Checks(); c.expect(0, 'a'); c.fail('b'); print('ran on'); c.finish()"
[ "$rc" = 1 ] && [ "$out" = "FAIL a
FAIL b
ran on
2 check(s) failed" ] || bad "py: Checks keeps going and finish fails (rc=$rc, out=$out)"

echo "OK testkit"
