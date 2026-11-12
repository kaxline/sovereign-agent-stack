#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# The AI text detector add-on's host commands. See docs/detector.md.
#
#   scripts/detector.sh init     create the bearer token if there is none
#   scripts/detector.sh rotate   replace the bearer token
#   scripts/detector.sh check    POST stdin's text to the host port, print the result
#
# The token is <data root>/addons/detector/secrets/http_token (0600). The
# detector service reads it on every request, so rotate takes effect at once.
# `addon.sh enable detector` runs init. check reads the port from
# DETECTOR_PORT (environment, then .env, else 8790), sends one paid request
# to the provider, and prints the JSON answer; it exits 1 on an error answer.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"

root="$(resolve_data_root_path "${ASSISTANT_DATA_ROOT:-}")"
validate_data_root_path "$root"
secrets="${root}/addons/detector/secrets"
token_file="${secrets}/http_token"

new_token() {
  mkdir -p "$secrets"
  chmod 700 "$secrets"
  local tmp
  tmp="$(mktemp "${secrets}/.http_token.XXXXXX")"
  chmod 600 "$tmp"
  # 32 random bytes as hex.
  od -An -N32 -tx1 /dev/urandom | tr -d ' \n' > "$tmp"
  mv -f "$tmp" "$token_file"
}

case "${1:-}" in
  init)
    if [[ -s "$token_file" ]]; then
      echo "[detector] token exists; kept" >&2
    else
      new_token
      echo "[detector] token written to ${token_file}" >&2
    fi
    ;;
  rotate)
    new_token
    echo "[detector] new token written to ${token_file}" >&2
    ;;
  check)
    [[ -s "$token_file" ]] || { echo "error: no token; run scripts/detector.sh init" >&2; exit 2; }
    port="${DETECTOR_PORT:-$(data_root_env_get DETECTOR_PORT 8790)}"
    body="$(mktemp)"
    trap 'rm -f "$body"' EXIT
    python3 -c 'import json,sys; json.dump({"text": sys.stdin.read()}, sys.stdout)' > "$body"
    out="$(curl -sS -X POST "http://127.0.0.1:${port}/check" \
      -H "Authorization: Bearer $(cat "$token_file")" \
      -H 'Content-Type: application/json' --data-binary "@${body}")"
    printf '%s\n' "$out"
    case "$out" in *'"error"'*) exit 1 ;; esac
    ;;
  *)
    sed -n '4,6s/^# *//p' "${BASH_SOURCE[0]}" >&2
    exit 2
    ;;
esac
