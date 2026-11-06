#!/usr/bin/env bash
# The first run (setup.sh, then crm.sh init and todo.sh init) needs only bash,
# the base system's tools and the container engine: no python3, node or make
# on the host. See CHANGELOG.md v0.3.11 and docs/extending.md.
#
# 1. Source check: the scripts on that path never call python3 or node
#    directly, outside the STACK_PYTHON=host / N8N_NODE=host branches.
# 2. Live check, when docker and the pinned Hermes image are available: with a
#    PATH built from /bin and /usr/bin minus python3, node and make, plus
#    docker, stack_python runs a script and sync-terminal-backend.sh writes
#    its override into a scratch root. NO_HOST_PYTHON_LIVE=1 makes a missing
#    docker or image a failure instead of a skip.
#
#   bash scripts/test-no-host-python.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
fail=0
bad() { printf 'FAIL %s\n' "$*" >&2; fail=1; }

FIRST_RUN="scripts/setup.sh scripts/sync-profiles.sh scripts/sync-signal-profile.sh
scripts/sync-buzz-profile.sh scripts/sync-terminal-backend.sh scripts/sync-n8n-credentials.sh
scripts/todo.sh scripts/crm.sh scripts/db.sh scripts/lib/env.sh scripts/lib/data-root.sh
scripts/lib/hermes-yaml.sh"

# A command position (line start, after ; & | ( or a keyword, after VAR=value
# prefixes) followed by python3, python or node.
cmd='(^|[;&|(]|\bthen|\bdo|\belse|\bif|\belif|\bwhile|\buntil|!)[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*(python3?|node)([[:space:]]|$)'
for f in $FIRST_RUN; do
  [[ -f "$f" ]] || { bad "${f} is listed but missing"; continue; }
  # Drop comments, then the two host-mode branches, which are opt-in.
  hits="$(sed -e 's/^[[:space:]]*#.*//' "$f" | grep -nE "$cmd" \
    | grep -vE 'N8N_NODE|node "\$@"' || true)"
  [[ -z "$hits" ]] || bad "${f} runs host python3/node: ${hits}"
  if grep -nE '^[[:space:]]*require_python3' "$f" >/dev/null; then
    bad "${f} calls require_python3"
  fi
done
[[ "$fail" -eq 0 ]] && echo "ok: first-run scripts run no host python3 or node"

# --- Live check ---
live_skip() {
  if [[ "${NO_HOST_PYTHON_LIVE:-}" == 1 ]]; then bad "live check: $*"; else echo "skip: live check ($*)"; fi
}
docker_bin="$(command -v docker || true)"
# shellcheck source=lib/python.sh
source "${ROOT}/scripts/lib/python.sh"
image="$(stack_hermes_image)"
if [[ -z "$docker_bin" ]] || ! docker info >/dev/null 2>&1; then
  live_skip "docker is not available"
elif ! docker image inspect "$image" >/dev/null 2>&1; then
  live_skip "${image} is not pulled"
else
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  mkdir -p "$tmp/bin" "$tmp/root"
  for dir in /bin /usr/bin; do
    for tool in "$dir"/*; do
      name="${tool##*/}"
      case "$name" in python|python3|python3.*|node|make) continue ;; esac
      [[ -e "$tmp/bin/$name" ]] || ln -s "$tool" "$tmp/bin/$name"
    done
  done
  # docker and its credential helpers, wherever they live.
  for tool in "$(dirname "$docker_bin")"/docker* "$(dirname "$docker_bin")"/podman*; do
    [[ -e "$tool" && ! -e "$tmp/bin/${tool##*/}" ]] && ln -s "$tool" "$tmp/bin/${tool##*/}"
  done
  # Make sure the trimmed PATH really lacks them.
  for name in python3 node make; do
    if PATH="$tmp/bin" command -v "$name" >/dev/null 2>&1; then bad "trimmed PATH still has ${name}"; fi
  done

  out="$(PATH="$tmp/bin" ROOT="$ROOT" /bin/bash -c '
    set -euo pipefail
    source scripts/lib/python.sh
    printf "import sys; print(sys.version_info >= (3, 9))\n" | stack_python -
  ' 2>&1 || true)"
  if [[ "$out" == True ]]; then
    echo "ok: stack_python runs without host python3"
  else
    bad "stack_python without host python3: ${out}"
  fi

  printf 'HERMES_TERMINAL_BACKEND=ssh\nCODING_EXTRA_ROOTS=%s/code\nCONTEXT_EXTRA_ROOTS=\n' "$tmp" > "$tmp/root/.env"
  if PATH="$tmp/bin" SYNC_TERMINAL_ROOT="$tmp/root" /bin/bash scripts/sync-terminal-backend.sh > "$tmp/sync.log" 2>&1 \
    && grep -q "$tmp/code:$tmp/code" "$tmp/root/docker-compose.override.yml"; then
    echo "ok: sync-terminal-backend.sh runs without host python3"
  else
    bad "sync-terminal-backend.sh without host python3: $(tail -3 "$tmp/sync.log")"
  fi
fi

[[ "$fail" -eq 0 ]] && echo "OK no host python3, node or make on the first run"
exit "$fail"
