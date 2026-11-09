# shellcheck shell=bash
# JSON-lines output for scripts/addon.sh --json, and the step runner.
# Source it, then call ev_init once. The sourcing script defines `cli`
# (addon_cli.py), which ev_fail uses for logTail.
#
# In JSON mode stdout carries one JSON object per line and nothing else:
# `setup` lines from ev_step, then one `done` (ev_done) or `error` (ev_fail)
# line. In human mode ev_step and ev_done print nothing, and step output goes
# to stderr. `error: MESSAGE` goes to stderr in both modes.
#
# Variables:
#   EV_JSON          1 in JSON mode, else 0.
#   EV_TMP           scratch directory for step logs, removed by ev_cleanup.
#   EV_LAST_LOG      the log of the last run_step (empty in human mode).
#   EV_STEP          the step that last printed running, or pull; a signal's
#                    error line names it.
#   EV_CUR_LOG       the log of the step running now, for that error line.
#   EV_ON_FAIL       a function ev_fail runs once before it prints; addon.sh
#                    sets it to its rollback while enable has flags to restore.
#   EV_ROLLED_BACK   set to 1 by that function when it restored .env; ev_fail
#                    then adds "rolledBack":true.
#
# ev_init saves stdout as file descriptor 3 and stderr as 4, and every line
# here is printed there. A trap runs inside whatever redirection the shell is
# in (run_step's log, say), so the plain stdout can't be trusted. A command
# run_step runs can print its own JSON lines inside its step on fd 3.
#
# bash 3.2 compatible.

EV_JSON=0
EV_TMP=""
EV_LAST_LOG=""
EV_CUR_LOG=""
EV_STEP=""
EV_CHILD=""
EV_ON_FAIL=""
EV_ROLLED_BACK=0

# ev_init JSON_MODE: JSON_MODE is 0 or 1. Makes EV_TMP and traps SIGINT and
# SIGTERM, which stop the running step and end with ev_fail.
ev_init() {
  EV_JSON="$1"
  EV_TMP="$(mktemp -d "${TMPDIR:-/tmp}/addon.XXXXXX")"
  exec 3>&1 4>&2
  trap 'ev_on_signal INT' INT
  trap 'ev_on_signal TERM' TERM
}

# ev_cleanup: removes EV_TMP. addon.sh calls it from its EXIT trap.
ev_cleanup() {
  [[ -n "$EV_TMP" ]] && rm -rf "$EV_TMP"
  return 0
}

# ev_log NAME: makes a new empty log under EV_TMP and prints its path. Callers
# use $(ev_log NAME), a subshell, so the name is unique through mktemp.
ev_log() {
  mktemp "${EV_TMP}/${1}.XXXXXX"
}

# json_str TEXT: TEXT as a JSON string literal. Tab becomes a space, other
# ASCII control characters are dropped and the result is cut to 500
# characters, as addon_cli.py clean_line does.
json_str() {
  local s
  s="$(printf '%s' "$1" | tr '\t' ' ' | tr -d '\000-\010\012-\037\177')"
  s="${s:0:500}"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '"%s"' "$s"
}

# ev_json_array CSV: a JSON array of the non-empty items of CSV.
ev_json_array() {
  local out="" item rest="$1"
  while [[ -n "$rest" ]]; do
    item="${rest%%,*}"
    if [[ "$rest" == *,* ]]; then rest="${rest#*,}"; else rest=""; fi
    [[ -n "$item" ]] && out="${out:+${out},}$(json_str "$item")"
  done
  printf '[%s]' "$out"
}

# ev_step STEP STATUS [TARGET]: a setup line. STATUS is running, done or error.
ev_step() {
  [[ "$2" == running ]] && EV_STEP="$1"
  [[ "$EV_JSON" == 1 ]] || return 0
  local target=""
  [[ -n "${3:-}" ]] && target=",\"target\":$(json_str "$3")"
  printf '{"phase":"setup","step":%s,"status":%s%s}\n' "$(json_str "$1")" "$(json_str "$2")" "$target" >&3
}

# ev_done ID ACTION RESTART_CSV MCP_CSV [ACCOUNT]: the done line.
ev_done() {
  [[ "$EV_JSON" == 1 ]] || return 0
  local account=""
  [[ -n "${5:-}" ]] && account=",\"account\":$(json_str "$5")"
  printf '{"phase":"done","id":%s,"action":%s,"restart":%s,"mcp":%s%s}\n' \
    "$(json_str "$1")" "$(json_str "$2")" "$(ev_json_array "$3")" "$(ev_json_array "$4")" "$account" >&3
}

# ev_exit_status CODE: the exit status for an error code. Codes other than
# addon.sh's own are a hook's error names, which exit 4.
ev_exit_status() {
  case "$1" in
    bad_input) echo 2 ;;
    missing_settings) echo 3 ;;
    compose_failed | busy) echo 1 ;;
    *) echo 4 ;;
  esac
}

# ev_log_tail LOGFILE: the last 20 lines of LOGFILE as a JSON array, or [].
ev_log_tail() {
  local tail
  tail="$(cli log-tail < "$1" 2> /dev/null)" || tail=""
  case "$tail" in
    "["*"]") printf '%s' "$tail" ;;
    *) printf '[]' ;;
  esac
}

# ev_fail CODE STEP MESSAGE LOGFILE: runs EV_ON_FAIL, prints the error line
# (JSON mode) and `error: MESSAGE` on stderr, and exits with CODE's status.
# An empty STEP is null in the error line (a failure before any step, such as
# busy or a usage error). LOGFILE may be empty.
ev_fail() {
  local code="$1" step="$2" message="$3" log="${4:-}" hook="$EV_ON_FAIL" tail="[]" stepjson=null extra=""
  trap '' INT TERM
  EV_ON_FAIL=""
  [[ -n "$hook" ]] && "$hook"
  if [[ "$EV_JSON" == 1 ]]; then
    [[ -n "$log" && -s "$log" ]] && tail="$(ev_log_tail "$log")"
    [[ -n "$step" ]] && stepjson="$(json_str "$step")"
    [[ "$EV_ROLLED_BACK" == 1 ]] && extra=',"rolledBack":true'
    printf '{"phase":"error","error":%s,"step":%s,"message":%s,"logTail":%s%s}\n' \
      "$(json_str "$code")" "$stepjson" "$(json_str "$message")" "$tail" "$extra" >&3 || true
  fi
  printf 'error: %s\n' "$message" >&4 || true
  exit "$(ev_exit_status "$code")"
}

# ev_kill_tree PID: stops PID and every process under it, by PID. The whole
# tree is listed first so a parent can't start a new child in between.
ev_kill_tree() {
  local pids="$1" queue="$1" pid kids
  while [[ -n "$queue" ]]; do
    pid="${queue%% *}"
    if [[ "$queue" == *" "* ]]; then queue="${queue#* }"; else queue=""; fi
    kids="$(pgrep -P "$pid" 2> /dev/null | tr '\n' ' ')" || kids=""
    kids="${kids% }"
    if [[ -n "$kids" ]]; then
      pids="${pids} ${kids}"
      queue="${queue:+${queue} }${kids}"
    fi
  done
  # shellcheck disable=SC2086
  kill $pids 2> /dev/null || true
}

# ev_on_signal NAME: the SIGINT/SIGTERM trap.
ev_on_signal() {
  trap '' INT TERM
  [[ -n "$EV_CHILD" ]] && ev_kill_tree "$EV_CHILD"
  EV_CHILD=""
  ev_fail command_failed "$EV_STEP" "stopped by SIG$1" "$EV_CUR_LOG"
}

# ev_wait CMD...: runs CMD in the background and waits for it, so the signal
# traps run while it is running. Returns CMD's status.
ev_wait() {
  local rc=0
  "$@" &
  EV_CHILD=$!
  wait "$EV_CHILD" || rc=$?
  EV_CHILD=""
  return "$rc"
}

# run_step STEP TARGET CMD...: CMD between ev_step running and done (or error),
# with stdout and stderr to a log under EV_TMP in JSON mode and to stderr in
# human mode. TARGET may be empty. Returns CMD's status and leaves the log path
# in EV_LAST_LOG (empty in human mode). CMD runs in a subshell when it is a
# function, so it can't set variables here.
run_step() {
  local step="$1" target="$2" rc=0
  shift 2
  ev_step "$step" running "$target"
  if [[ "$EV_JSON" == 1 ]]; then
    EV_CUR_LOG="$(ev_log "$step")"
    ev_wait "$@" > "$EV_CUR_LOG" 2>&1 || rc=$?
  else
    EV_CUR_LOG=""
    ev_wait "$@" >&4 || rc=$?
  fi
  EV_LAST_LOG="$EV_CUR_LOG"
  EV_CUR_LOG=""
  if [[ "$rc" == 0 ]]; then
    ev_step "$step" "done" "$target"
  else
    ev_step "$step" error "$target"
    # Keep the log for an ev_fail that a signal brings in before the caller's.
    EV_CUR_LOG="$EV_LAST_LOG"
  fi
  return "$rc"
}
