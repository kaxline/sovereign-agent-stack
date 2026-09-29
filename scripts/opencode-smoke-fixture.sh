#!/usr/bin/env bash
# Create the opencode-smoke fixture under OPENCODE_WORKSPACE_HOST for Hermes
# coding_* delegation smoke tests. See docs/opencode.md#smoke-test.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"

env_get() {
  local key="$1" default="${2:-}"
  [[ -f "$ENV_FILE" ]] || { echo "$default"; return; }
  local line
  line="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 || true)"
  [[ -z "$line" ]] && { echo "$default"; return; }
  local val="${line#*=}"
  val="${val%\"}"; val="${val#\"}"
  echo "$val"
}

PRIMARY="$(env_get OPENCODE_WORKSPACE_HOST)"
[[ -n "$PRIMARY" ]] || { echo "error: OPENCODE_WORKSPACE_HOST unset in .env" >&2; exit 1; }
[[ "$PRIMARY" == /* ]] || { echo "error: OPENCODE_WORKSPACE_HOST must be absolute" >&2; exit 1; }

SMOKE="${PRIMARY}/opencode-smoke"
mkdir -p "$SMOKE/src" "$SMOKE/tests"
printf '%s\n' 'def greet(name: str) -> str:' '    raise NotImplementedError' >"$SMOKE/src/greet.py"
printf '%s\n' \
  'import sys' \
  'from pathlib import Path' \
  'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))' \
  'from greet import greet' \
  '' \
  'def test_greet_ada():' \
  '    assert greet("Ada") == "Hello, Ada!"' \
  >"$SMOKE/tests/test_greet.py"
touch "$SMOKE/src/__init__.py" "$SMOKE/tests/__init__.py"
printf '%s\n' '[pytest]' 'pythonpath = src' >"$SMOKE/pytest.ini"

if [[ ! -d "$SMOKE/.git" ]]; then
  git -C "$SMOKE" init -q
fi
git -C "$SMOKE" add -A
git -C "$SMOKE" -c user.email=smoke@local -c user.name=smoke \
  commit -q -m "smoke fixture" --allow-empty-message 2>/dev/null \
  || git -C "$SMOKE" -c user.email=smoke@local -c user.name=smoke \
    commit -q --allow-empty -m "smoke fixture (refresh)"

echo "Smoke fixture ready: $SMOKE"
echo
echo "In Hermes WebUI, ask:"
echo "  In $SMOKE, implement greet(name: str) -> str returning Hello, {name}!,"
echo "  fix the pytest so greet(\"Ada\") == \"Hello, Ada!\", and run the tests until they pass."
