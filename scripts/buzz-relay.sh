#!/usr/bin/env bash
# Manage a local Buzz community relay started from BUZZ_LOCAL_DIR_PATH via `just relay`.
# Daemonizes the long-running process so Make / the shell stay free.
#
#   make buzz-relay-start
#   make buzz-relay-stop
#   make buzz-relay-status
#   make buzz-relay-logs
#   ./scripts/buzz-relay.sh start|stop|status|logs|restart
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

STATE_DIR="${ROOT}/data/hermes/.cache/buzz-relay"
PIDFILE="${STATE_DIR}/relay.pid"
LOGFILE="${STATE_DIR}/relay.log"

log() { printf '[buzz-relay] %s\n' "$*"; }
die() { printf '[buzz-relay] ERROR: %s\n' "$*" >&2; exit 1; }

# Return 0 if something is listening on TCP port (default 3000).
port_listening() {
  local port="${1:-3000}"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
    return $?
  fi
  if command -v nc >/dev/null 2>&1; then
    nc -z 127.0.0.1 "$port" >/dev/null 2>&1
    return $?
  fi
  return 1
}

listener_detail() {
  local port="${1:-3000}"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print $1,$2,$9}' | head -5 | tr '\n' ';'
  fi
}

usage() {
  cat <<'EOF'
Usage: ./scripts/buzz-relay.sh <command>

Commands:
  start     Start `just relay` in the background (from BUZZ_LOCAL_DIR_PATH)
  stop      Stop the background relay
  restart   stop + start
  status    Print running / stopped (+ pid, log path)
  logs      Tail the relay log (Ctrl-C to stop)

Requires BUZZ_LOCAL_DIR_PATH in root .env (path to a Buzz checkout with a Justfile).
Typical make wrappers:
  make buzz-relay-start
  make buzz-relay-stop
  make buzz-relay-status
  make buzz-relay-logs
EOF
}

env_get() {
  local key="$1"
  local default="${2:-}"
  local val=""
  if [[ -f .env ]]; then
    val="$(grep -E "^${key}=" .env 2>/dev/null | head -1 | cut -d= -f2- || true)"
    val="${val%\"}"
    val="${val#\"}"
    val="${val%\'}"
    val="${val#\'}"
  fi
  if [[ -z "$val" ]]; then
    echo "$default"
  else
    echo "$val"
  fi
}

expand_user_path() {
  local raw="$1"
  [[ -n "$raw" ]] || return 1
  if [[ "$raw" == "~" ]]; then
    raw="$HOME"
  elif [[ "$raw" == "~/"* ]]; then
    raw="$HOME/${raw#~/}"
  fi
  if [[ "$raw" != /* ]]; then
    raw="${ROOT}/${raw#./}"
  fi
  raw="${raw%/}"
  if [[ -d "$raw" ]]; then
    (cd "$raw" && pwd -P)
  else
    echo "$raw"
  fi
}

resolve_buzz_dir() {
  local raw
  raw="$(env_get BUZZ_LOCAL_DIR_PATH)"
  [[ -n "$raw" ]] || die "BUZZ_LOCAL_DIR_PATH is unset — add it to .env (path to your Buzz checkout)"
  local dir
  dir="$(expand_user_path "$raw")"
  [[ -d "$dir" ]] || die "BUZZ_LOCAL_DIR_PATH does not exist: ${dir}"
  [[ -f "${dir}/justfile" || -f "${dir}/Justfile" ]] \
    || die "No Justfile in ${dir} — is BUZZ_LOCAL_DIR_PATH the Buzz repo root?"
  command -v just >/dev/null 2>&1 || die "'just' not found on PATH — install https://github.com/casey/just"
  echo "$dir"
}

read_pid() {
  if [[ -f "$PIDFILE" ]]; then
    local pid
    pid="$(tr -d '[:space:]' < "$PIDFILE" || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]]; then
      echo "$pid"
      return 0
    fi
  fi
  return 1
}

is_alive() {
  local pid="$1"
  kill -0 "$pid" 2>/dev/null
}

# Recursively signal a process and its descendants (just often spawns the real relay).
signal_tree() {
  local sig="$1"
  local pid="$2"
  local child
  while IFS= read -r child; do
    [[ -n "$child" ]] || continue
    signal_tree "$sig" "$child"
  done < <(pgrep -P "$pid" 2>/dev/null || true)
  kill "-${sig}" "$pid" 2>/dev/null || true
}

cmd_status() {
  local pid=""
  local listening=0
  local listen_info=""
  if port_listening 3000; then
    listening=1
    listen_info="$(listener_detail 3000)"
  fi
  if pid="$(read_pid)" && is_alive "$pid"; then
    log "running (pid ${pid})"
    if [[ "$listening" -eq 1 ]]; then
      log "listening on TCP 3000 (${listen_info})"
    else
      log "NOT listening on TCP 3000 yet (likely still compiling — see log)"
    fi
    log "log: ${LOGFILE}"
    return 0
  fi
  if [[ -n "${pid:-}" ]]; then
    log "stopped (stale pidfile ${pid})"
    rm -f "$PIDFILE"
  else
    log "stopped"
  fi
  if [[ "$listening" -eq 1 ]]; then
    log "NOTE: something else is listening on 3000 (${listen_info})"
  fi
  return 1
}

cmd_start() {
  local dir
  dir="$(resolve_buzz_dir)"

  local pid=""
  if pid="$(read_pid)" && is_alive "$pid"; then
    log "already running (pid ${pid})"
    return 0
  fi
  rm -f "$PIDFILE"

  mkdir -p "$STATE_DIR"
  : >"$LOGFILE"

  log "starting just relay in ${dir}"
  (
    cd "$dir"
    # nohup + redirect keeps the relay alive after Make exits.
    nohup just relay >>"$LOGFILE" 2>&1 &
    echo $! >"$PIDFILE"
  )

  # Brief settle so we can detect immediate failure.
  sleep 1
  local listening=0
  port_listening 3000 && listening=1
  if pid="$(read_pid)" && is_alive "$pid"; then
    log "started (pid ${pid})"
    log "log: ${LOGFILE}"
    if [[ "$listening" -eq 1 ]]; then
      log "listening on TCP 3000 ($(listener_detail 3000))"
    else
      log "process up but NOT yet listening on TCP 3000 (cargo build may still be running)"
    fi
    log "BUZZ_RELAY_URL should point at this relay (often ws://localhost:3000)"
  else
    rm -f "$PIDFILE"
    die "failed to stay up — check ${LOGFILE}"
  fi
}

cmd_stop() {
  local pid=""
  if ! pid="$(read_pid)"; then
    log "not running"
    return 0
  fi
  if ! is_alive "$pid"; then
    log "not running (cleared stale pidfile)"
    rm -f "$PIDFILE"
    return 0
  fi

  log "stopping pid ${pid}"
  signal_tree TERM "$pid"
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if ! is_alive "$pid"; then
      break
    fi
    sleep 0.3
  done
  if is_alive "$pid"; then
    log "still alive — sending KILL"
    signal_tree KILL "$pid"
    sleep 0.2
  fi
  rm -f "$PIDFILE"
  if is_alive "$pid"; then
    die "could not stop pid ${pid}"
  fi
  log "stopped"
}

cmd_logs() {
  mkdir -p "$STATE_DIR"
  touch "$LOGFILE"
  log "tailing ${LOGFILE} (Ctrl-C to stop)"
  exec tail -n 80 -f "$LOGFILE"
}

cmd="${1:-}"
case "$cmd" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status) cmd_status || true ;;
  logs) cmd_logs ;;
  -h|--help|help|"") usage; [[ -n "$cmd" ]] || exit 1 ;;
  *) die "unknown command: ${cmd} (try: start|stop|status|logs|restart)" ;;
esac
