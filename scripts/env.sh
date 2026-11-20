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
# running the same command twice leaves the same file. It holds the config
# lock addon.sh uses (scripts/lib/stack-lock.sh) while it writes, so two
# clients changing .env at once apply one after the other: set STACK_CLIENT to
# name the caller and STACK_LOCK_TIMEOUT to wait for a busy lock. A lock still
# busy then is error busy (exit 1) with "holder":{"pid","client","since"}. The change takes effect
# the next time the affected services are recreated; this script starts and
# stops nothing.
#
# --json prints one line in addon.sh's shapes: {"phase":"done","key":K,
# "changed":true|false}, or {"phase":"error","error":REASON,"step":null,
# "message":TEXT,"logTail":[]} and exit 2 for bad_input, not_allowed and
# use_addon_sh. Without --json stdout is empty and errors go to stderr.
#
# bash 3.2 compatible.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
# shellcheck source=lib/json-lines.sh
source "${ROOT}/scripts/lib/json-lines.sh"
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"
# shellcheck source=lib/stack-lock.sh
source "${ROOT}/scripts/lib/stack-lock.sh"
jl_init

args=()
# shellcheck disable=SC2034  # JL_JSON is read by lib/json-lines.sh
for a in "$@"; do
  case "$a" in
    --json) JL_JSON=1 ;;
    *) args+=("$a") ;;
  esac
done

die() { jl_fail "$1" "" "$2"; }

usage() {
  printf 'Usage: scripts/env.sh set KEY VALUE [--json] | unset KEY [--json]\n' >&2
  exit 2
}

# client_writable KEY: whether a client may set KEY. Secrets the stack
# generates (LLM_PROXY_ADMIN_TOKEN) stay out. LIGHTRAG_LLM_* and EMBEDDING_*
# are the model choices setup.sh writes beside LLM_* (CR 0037 R6).
client_writable() {
  case "$1" in
    LLM_PROXY_ADMIN_TOKEN) return 1 ;;
    LLM_* | LIGHTRAG_LLM_* | EMBEDDING_* | *_IMAGE | *_IMAGE_TAG | HERMES_TERMINAL_BACKEND | CODING_* | CONTEXT_* | CALDAV_TZ) return 0 ;;
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

done_line() { jl_done "\"key\":\"$1\",\"changed\":$2"; }

# lock: takes the config lock, or ends with busy.
lock() {
  local data_root rc=0 who
  data_root="$(resolve_data_root_path "${ASSISTANT_DATA_ROOT:-$(env_get ASSISTANT_DATA_ROOT)}")"
  stack_lock "${data_root}/addons/.lock" || rc=$?
  case "$rc" in
    0) trap stack_unlock EXIT ;;
    2) jl_fail busy "" "${data_root}/addons/.lock is not a lock symlink; remove it if no change is running" ;;
    *)
      who="$(stack_lock_busy_text)"
      jl_fail busy "" "another config change is running${who:+ (${who})}" "" ",\"holder\":$(stack_lock_holder_json)" ;;
  esac
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
    lock
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
    lock
    if ! has_key "$key"; then
      done_line "$key" false
      exit 0
    fi
    remove_env .env "$key"
    done_line "$key" true
    ;;
  *) usage ;;
esac
