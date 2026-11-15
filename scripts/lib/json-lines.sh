# shellcheck shell=bash
# JSON lines in addon.sh's shapes (docs/extending.md "JSON lines"), for the
# host scripts that aren't addon.sh: env.sh, backup.sh, upgrade.sh.
# scripts/install.sh carries its own copy, since it runs without the stack.
#
#   {"phase":"setup","step":S,"status":"running"|"done"[,...]}
#   {"phase":"done",...}
#   {"phase":"error","error":CODE,"step":S|null,"message":M,"logTail":[...][,...]}
#
# Source it, set JL_JSON=1 for JSON mode, and call jl_init once: it saves
# stdout as fd 3, so a line printed inside $(...) still reaches the caller.
# Exit codes follow addon.sh: bad_input 2, command_failed 4, anything else 1;
# env.sh's refusals not_allowed and use_addon_sh are 2 like bad_input.
#
# bash 3.2 compatible.

JL_JSON="${JL_JSON:-0}"

jl_init() { exec 3>&1; }

# jl_str TEXT: TEXT as a JSON string: backslash and quote escaped, tab to a
# space, other control characters dropped, cut to 500 bytes without leaving
# half a UTF-8 character.
jl_str() {
  local s
  s="$(printf '%s' "$1" | LC_ALL=C tr '\t' ' ' | LC_ALL=C tr -d '\000-\010\012-\037\177' | LC_ALL=C cut -b1-500 | { iconv -c -f UTF-8 -t UTF-8 2> /dev/null || cat; })"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '"%s"' "$s"
}

# jl_str_or_null TEXT: jl_str, or null for an empty TEXT.
jl_str_or_null() { if [[ -n "$1" ]]; then jl_str "$1"; else printf 'null'; fi; }

# jl_tail FILE: up to 20 last lines of FILE as a JSON array.
jl_tail() {
  local line out="" first=1
  [[ -n "${1:-}" && -s "${1:-}" ]] || { printf '[]'; return; }
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$first" == 1 ]] || out="${out},"
    first=0
    out="${out}$(jl_str "$line")"
  done < <(tail -20 "$1")
  printf '[%s]' "$out"
}

jl_emit() { [[ "$JL_JSON" == 1 ]] && printf '%s\n' "$1" >&3; return 0; }

# jl_setup STEP STATUS [EXTRA]: EXTRA is more members, starting with a comma.
jl_setup() { jl_emit "{\"phase\":\"setup\",\"step\":$(jl_str "$1"),\"status\":\"$2\"${3:-}}"; }

# jl_done MEMBERS: MEMBERS without braces, e.g. "\"key\":\"X\"".
jl_done() { jl_emit "{\"phase\":\"done\"${1:+,$1}}"; }

jl_exit_code() {
  case "$1" in
    bad_input | not_allowed | use_addon_sh) echo 2 ;;
    command_failed) echo 4 ;;
    *) echo 1 ;;
  esac
}

# jl_fail CODE STEP MESSAGE [LOGFILE] [EXTRA]: prints the error line (JSON
# mode) and `error: MESSAGE` on stderr, and exits with CODE's status.
jl_fail() {
  local code="$1" step="$2" msg="$3" log="${4:-}" extra="${5:-}"
  jl_emit "{\"phase\":\"error\",\"error\":\"${code}\",\"step\":$(jl_str_or_null "$step"),\"message\":$(jl_str "$msg"),\"logTail\":$(jl_tail "$log")${extra}}"
  printf 'error: %s\n' "$msg" >&2
  exit "$(jl_exit_code "$code")"
}
