#!/usr/bin/env bash
# Set or unset a client-writable key in the project .env.
#
#   scripts/env.sh set KEY VALUE [--json]
#   scripts/env.sh unset KEY [--json]
#
# Clients (Boundary, locally or over SSH) use this instead of editing .env, so
# they don't depend on its layout. Only the keys in client_writable may be
# changed. Add-on switches (*_ENABLED) and COMPOSE_PROFILES belong to
# scripts/addon.sh, which also syncs profiles and starts or stops services.
#
# Each change replaces .env atomically (scripts/lib/env.sh replace_file), and
# running the same command twice leaves the same file. The change takes effect
# the next time the affected services are recreated; this script starts and
# stops nothing.
#
# --json prints one line: {"event":"done","key":K,"changed":true|false}, or
# {"event":"error","error":REASON,"message":TEXT} and exit 2 for bad_input,
# not_allowed and use_addon_sh. Without --json stdout is empty and errors go to
# stderr.
#
# bash 3.2 compatible.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"

JSON=0
args=()
for a in "$@"; do
  case "$a" in
    --json) JSON=1 ;;
    *) args+=("$a") ;;
  esac
done

json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '%s' "$s"
}

die() {
  local reason="$1" msg="$2"
  if [[ "$JSON" == 1 ]]; then
    printf '{"event":"error","error":"%s","message":"%s"}\n' "$reason" "$(json_escape "$msg")"
  fi
  printf 'error: %s\n' "$msg" >&2
  exit 2
}

usage() {
  printf 'Usage: scripts/env.sh set KEY VALUE [--json] | unset KEY [--json]\n' >&2
  exit 2
}

# client_writable KEY: whether a client may set KEY. Secrets the stack
# generates (LLM_PROXY_ADMIN_TOKEN) stay out.
client_writable() {
  case "$1" in
    LLM_PROXY_ADMIN_TOKEN) return 1 ;;
    LLM_* | *_IMAGE | *_IMAGE_TAG | HERMES_TERMINAL_BACKEND | CODING_* | CONTEXT_*) return 0 ;;
  esac
  return 1
}

check_key() {
  local key="$1"
  [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || die bad_input "not a .env key: ${key}"
  case "$key" in
    COMPOSE_PROFILES | *_ENABLED)
      die use_addon_sh "${key} is an add-on switch; use scripts/addon.sh enable or disable" ;;
  esac
  client_writable "$key" || die not_allowed "${key} is not a client-writable key"
}

# current KEY: the raw value of KEY's first line, and whether there is one.
has_key() { [[ -f .env ]] && grep -q -E "^$1=" .env; }
raw_value() { grep -m 1 -E "^$1=" .env | cut -d= -f2-; }

done_line() {
  [[ "$JSON" == 1 ]] || return 0
  printf '{"event":"done","key":"%s","changed":%s}\n' "$1" "$2"
}

[[ ${#args[@]} -ge 2 ]] || usage
cmd="${args[0]}"
key="${args[1]}"
case "$cmd" in
  set)
    [[ ${#args[@]} -eq 3 ]] || usage
    value="${args[2]}"
    check_key "$key"
    case "$value" in
      *$'\n'* | *$'\r'*) die bad_input "the value for ${key} has a line break" ;;
    esac
    if has_key "$key" && [[ "$(raw_value "$key")" == "$value" ]] \
      && [[ "$(grep -c -E "^${key}=" .env)" == 1 ]]; then
      done_line "$key" false
      exit 0
    fi
    upsert_env .env "$key" "$value"
    done_line "$key" true
    ;;
  unset)
    [[ ${#args[@]} -eq 2 ]] || usage
    check_key "$key"
    if ! has_key "$key"; then
      done_line "$key" false
      exit 0
    fi
    remove_env .env "$key"
    done_line "$key" true
    ;;
  *) usage ;;
esac
