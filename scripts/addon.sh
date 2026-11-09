#!/usr/bin/env bash
# List, configure, enable, disable and run built-in add-ons (addons/<id>/addon.json).
# This is the interface Boundary calls; scripts/lib/addon_cli.py does the
# parsing and file work in the Hermes image (stack_python), so the host needs
# only bash and the container engine.
#
#   scripts/addon.sh list --json
#   scripts/addon.sh configure <short-id> [--instance <slug>] < values.json
#   scripts/addon.sh configure <short-id> --instance <slug> --remove
#   scripts/addon.sh enable <short-id> [--json]
#   scripts/addon.sh disable <short-id> [--purge] [--json]
#   scripts/addon.sh run <short-id> <command> [--instance <slug>]
#
# configure reads one JSON object on stdin. Secret values go only to
# data/addons/<short-id>/secrets; bool settings with a flag write that flag to
# .env while the add-on is enabled. enable checks every required setting, sets builtin.flag=1 (and each
# gate flag a stored setting owns), syncs COMPOSE_PROFILES, starts the
# services whose gate is on and reloads Hermes. disable sets the flag and every
# gate flag to 0, syncs, stops the services and reloads; --purge also deletes
# data/addons/<short-id> and the compose volumes in services.*.volumes.
#
# --json prints one JSON object per line on stdout (scripts/lib/addon-events.sh):
# `pull` lines (enable only: it pulls the images first), `setup` lines per
# step, then one `done` or `error` line. Step output goes to stderr without
# --json.
#
# configure, enable and disable hold data/addons/.lock (a symlink to their PID)
# while they run; a second call fails with busy. A failed or interrupted enable writes the flags back as
# they were, syncs COMPOSE_PROFILES and stops the services if the add-on was
# off, and its error line has "rolledBack":true.
#
# Exit codes: 0 success, 1 compose_failed or busy, 2 bad_input (invalid input
# or usage), 3 missing_settings, 4 command_failed (run: docker compose run
# failed; addon_cli.py failed; SIGINT or SIGTERM).
#
# ADDONS_DIR overrides addons/ (tests). STACK_PYTHON=host uses host python3.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"
# shellcheck source=lib/python.sh
source "${ROOT}/scripts/lib/python.sh"
# shellcheck source=lib/addon-events.sh
source "${ROOT}/scripts/lib/addon-events.sh"

# usage: the synopsis from the header, on stdout.
usage() { sed -n '7,12s/^# *//p' "${BASH_SOURCE[0]}"; }
# usage_error REASON: `error: usage: REASON` and the synopsis on stderr, exit 2.
# In JSON mode, a bad_input error line instead.
usage_error() {
  [[ "$EV_JSON" == 1 ]] && ev_fail bad_input "" "usage: $1" ""
  printf 'error: usage: %s\n' "$1" >&2
  usage >&2
  exit 2
}

truthy() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

ADDONS="${ADDONS_DIR:-${ROOT}/addons}"
DATA_ROOT="$(resolve_data_root_path)"
validate_data_root_path "$DATA_ROOT" || exit 1

# cli ARGS...: addon_cli.py in the Hermes image with the data root (and an
# add-ons dir outside the repo) mounted. stdin passes through.
cli() {
  local mounts=(--mount "$DATA_ROOT")
  path_is_inside "$ADDONS" "$ROOT" || mounts+=(--mount "$ADDONS")
  stack_python "${mounts[@]}" "${ROOT}/scripts/lib/addon_cli.py" \
    --addons-dir "$ADDONS" --data-root "$DATA_ROOT" "$@"
}

# json_list KEY JSON: the items of a flat string list, one per line. addon_cli
# prints compact JSON and checks that names hold no quote, comma or bracket.
json_list() {
  printf '%s' "$2" | sed -n "s/.*\"$1\":\[\([^]]*\)\].*/\1/p" | tr ',' '\n' | tr -d '"' | sed '/^$/d'
}

# json_flags JSON: "FLAG VALUE" per line from {"flags":{"FLAG":"1",...}}.
json_flags() {
  printf '%s' "$1" | sed -n 's/.*"flags":{\([^}]*\)}.*/\1/p' | tr ',' '\n' \
    | sed -n 's/^"\([A-Z][A-Z0-9_]*\)":"\([01]\)"$/\1 \2/p'
}

write_flags() {
  local flag value
  while read -r flag value; do
    [[ -n "$flag" ]] || continue
    upsert_env .env "$flag" "$value"
  done <<EOF_FLAGS
$(json_flags "$1")
EOF_FLAGS
}

# .env flags as {"NAME":"1"|"0"} for every line whose value is a bool word.
# The first line for a name wins, as in env_get.
env_flags_json() {
  local out="" seen="," key val
  [[ -f .env ]] || { printf '{}'; return; }
  while IFS='=' read -r key val; do
    [[ "$seen" == *",${key},"* ]] && continue
    seen="${seen}${key},"
    val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
    case "$(printf '%s' "$val" | tr '[:upper:]' '[:lower:]')" in
      1 | true | yes | on) val=1 ;;
      0 | false | no | off | "") val=0 ;;
      *) continue ;;
    esac
    out="${out:+${out},}\"${key}\":\"${val}\""
  done < <(grep -E '^[A-Z][A-Z0-9_]*=' .env || true)
  printf '{%s}' "$out"
}

# Compose resolves the project from COMPOSE_PROJECT_NAME (environment, then
# .env), else the directory name, lowercased, keeping [a-z0-9_-].
compose_project() {
  local p="${COMPOSE_PROJECT_NAME:-$(env_get COMPOSE_PROJECT_NAME)}"
  [[ -n "$p" ]] || p="$(basename "$ROOT" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
  printf '%s\n' "$p"
}

hermes_reload() {
  make hermes-reload
}

# cli_checked STEP ARGS...: cli with stdin from /dev/null and its stdout in
# CLI_OUT. On failure, ev_fail with cli's own error line as the message:
# exit 2 is bad_input, 3 missing_settings, anything else command_failed.
CLI_OUT=""
cli_checked() {
  local step="$1" err rc=0 msg code
  shift
  err="$(ev_log cli)"
  CLI_OUT="$(cli "$@" < /dev/null 2> "$err")" || rc=$?
  [[ "$rc" == 0 ]] && return 0
  msg="$(sed -n 's/^error: //p' "$err" | tail -n 1)"
  [[ -n "$msg" ]] || msg="addon_cli.py $1 failed (exit ${rc})"
  case "$rc" in
    2) code=bad_input ;;
    3) code=missing_settings ;;
    *) code=command_failed ;;
  esac
  ev_fail "$code" "$step" "$msg" "$err"
}

# The lock: data/addons/.lock, a symlink whose target is the owner's PID.
# `ln -s` creates it and its PID in one atomic step, so a lock always names
# its owner. A lock whose PID no longer runs is taken over under a guard
# directory, data/addons/.lock.takeover: whoever makes the guard re-reads the
# link, replaces it only if it still names the same dead PID, and removes the
# guard. A guard older than 10 seconds belongs to a takeover that died and is
# removed. Anything else (a live owner, a held guard, a link replaced in the
# meantime) is busy. addon_unlock, from the EXIT trap, removes the link only
# while it still names this process, and the guard if this process holds it.
LOCK_PATH=""
LOCK_GUARD=""
# path_age PATH: seconds since PATH's mtime.
path_age() {
  local m
  m="$(stat -c %Y "$1" 2> /dev/null || stat -f %m "$1" 2> /dev/null)" || return 1
  [[ "$m" =~ ^[0-9]+$ ]] || return 1
  echo $(($(date +%s) - m))
}
lock_busy() {
  ev_fail busy "" "another add-on change is running${1:+ (pid $1)}" ""
}
addon_lock() {
  local lock="${DATA_ROOT}/addons/.lock" guard="${DATA_ROOT}/addons/.lock.takeover" pid age took=0
  mkdir -p "${DATA_ROOT}/addons"
  # ln -s onto a directory would create the link inside it.
  if [[ -e "$lock" && ! -L "$lock" ]]; then
    ev_fail busy "" "${lock} exists and is not a lock symlink; remove it if no add-on change is running" ""
  fi
  if ln -sn "$$" "$lock" 2> /dev/null; then
    LOCK_PATH="$lock"
    return 0
  fi
  pid="$(readlink "$lock" 2> /dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || lock_busy "$pid"
  kill -0 "$pid" 2> /dev/null && lock_busy "$pid"
  if age="$(path_age "$guard")" && [[ "$age" -gt 10 ]]; then
    rmdir "$guard" 2> /dev/null || true
  fi
  mkdir "$guard" 2> /dev/null || lock_busy ""
  LOCK_GUARD="$guard"
  if [[ "$(readlink "$lock" 2> /dev/null || true)" == "$pid" ]] && rm -f "$lock" \
    && ln -sn "$$" "$lock" 2> /dev/null; then
    took=1
    LOCK_PATH="$lock"
  fi
  rmdir "$guard" 2> /dev/null || true
  LOCK_GUARD=""
  [[ "$took" == 1 ]] || lock_busy ""
}
addon_unlock() {
  if [[ -n "$LOCK_PATH" && "$(readlink "$LOCK_PATH" 2> /dev/null || true)" == "$$" ]]; then
    rm -f "$LOCK_PATH"
  fi
  LOCK_PATH=""
  if [[ -n "$LOCK_GUARD" ]]; then
    rmdir "$LOCK_GUARD" 2> /dev/null || true
  fi
  LOCK_GUARD=""
}
on_exit() {
  addon_unlock
  ev_cleanup
}

# apply_flags PLAN: writes the plan's flags to .env and syncs COMPOSE_PROFILES.
apply_flags() {
  write_flags "$1"
  "${ROOT}/scripts/sync-profiles.sh"
}

# Rollback for enable. rollback_record PLAN saves each plan flag's .env line
# (or that it had none) and arms rollback; EV_ON_FAIL runs it on any failure,
# including SIGINT and SIGTERM.
ROLLBACK_FLAGS=""
ROLLBACK_STOP=0
ROLLBACK_SERVICES=""
rollback_record() {
  local plan="$1" flag value line builtin
  ROLLBACK_FLAGS=""
  while read -r flag value; do
    [[ -n "$flag" ]] || continue
    if line="$(grep -m 1 -E "^${flag}=" .env 2> /dev/null)"; then
      ROLLBACK_FLAGS="${ROLLBACK_FLAGS}${flag} 1 ${line#*=}"$'\n'
    else
      ROLLBACK_FLAGS="${ROLLBACK_FLAGS}${flag} 0"$'\n'
    fi
  done <<EOF_FLAGS
$(json_flags "$plan")
EOF_FLAGS
  builtin="$(printf '%s' "$plan" | sed -n 's/.*"builtin":"\([A-Z][A-Z0-9_]*\)".*/\1/p')"
  ROLLBACK_STOP=0
  truthy "$(env_get "$builtin")" || ROLLBACK_STOP=1
  ROLLBACK_SERVICES="$(json_list services "$plan" | sed 's/@.*//')"
  EV_ON_FAIL=rollback
}

# rollback: puts the recorded .env lines back, syncs and stops the services if
# the add-on was off. Output goes to a log (JSON mode) or stderr (fd 4, as it
# may run from a trap inside a step's redirection).
rollback() {
  if [[ "$EV_JSON" == 1 ]]; then
    rollback_apply > "$(ev_log rollback)" 2>&1 || true
  else
    rollback_apply >&4 2>&4 || true
  fi
}
rollback_apply() {
  local flag present value services=()
  while read -r flag present value; do
    [[ -n "$flag" ]] || continue
    if [[ "$present" == 1 ]]; then
      upsert_env .env "$flag" "$value"
    else
      remove_env .env "$flag"
    fi
  done <<EOF_FLAGS
$ROLLBACK_FLAGS
EOF_FLAGS
  EV_ROLLED_BACK=1
  "${ROOT}/scripts/sync-profiles.sh" || true
  if [[ "$ROLLBACK_STOP" == 1 ]]; then
    while IFS= read -r flag; do
      [[ -n "$flag" ]] && services+=("$flag")
    done <<EOF_SERVICES
$ROLLBACK_SERVICES
EOF_SERVICES
    [[ ${#services[@]} -gt 0 ]] && docker compose stop "${services[@]}"
  fi
  return 0
}

# remove_env FILE KEY: deletes every KEY= line.
remove_env() {
  local tmp
  [[ -f "$1" ]] || return 0
  tmp="$(mktemp)"
  grep -v -E "^$2=" "$1" > "$tmp" || true
  mv "$tmp" "$1"
}

# gate_on PLAN GATE: the gate's value after the plan's flags are written.
gate_on() {
  local v
  v="$(printf '%s' "$1" | sed -n "s/.*\"$2\":\"\([01]\)\".*/\1/p")"
  [[ -n "$v" ]] || v="$(env_get "$2")"
  truthy "$v"
}

# pull_stream LOG SERVICES...: one compose pull per service, its output
# appended to LOG and framed by marker objects so pull-events can name the
# service. Compose 2.2x labels events with the service; newer versions label
# them "Image <ref>". Only missing images are pulled (enable isn't an upgrade,
# and a machine offline or with a local-only image must still enable), and
# images with a build section are left to `up`.
pull_stream() {
  local log="$1" s
  shift
  for s in "$@"; do
    printf '{"addonPull":"start","service":"%s"}\n' "$s"
    docker compose --progress json pull --policy missing --ignore-buildable "$s" 2>&1 | tee -a "$log" || return 1
    printf '{"addonPull":"done","service":"%s"}\n' "$s"
  done
}
# pull_images SERVICES...: JSON mode only. pull lines on stdout, compose's
# output in a log; a failure is compose_failed at step pull.
pull_images() {
  local csv log rc=0
  [[ "$EV_JSON" == 1 && $# -gt 0 ]] || return 0
  csv="$(IFS=,; printf '%s' "$*")"
  log="$(ev_log pull)"
  EV_STEP=pull
  EV_CUR_LOG="$log"
  ev_wait pull_pipe "$log" "$csv" "$@" || rc=$?
  EV_CUR_LOG=""
  [[ "$rc" == 0 ]] || ev_fail compose_failed pull "docker compose pull failed for: $*" "$log"
}
pull_pipe() {
  local log="$1" csv="$2"
  shift 2
  pull_stream "$log" "$@" | cli pull-events "$csv"
}

cmd_list() {
  [[ "${1:-}" == "--json" && $# -eq 1 ]] || usage_error "list takes --json only"
  cli list --flags "$(env_flags_json)" < /dev/null
}

cmd_configure() {
  local sid="${1:-}" instance="" remove=0 out rc=0 flags
  [[ -n "$sid" ]] || usage_error "configure needs a short id"
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --instance) [[ $# -ge 2 ]] || usage_error "--instance needs a slug"; instance="$2"; shift 2 ;;
      --remove) remove=1; shift ;;
      *) usage_error "configure: unknown option $1" ;;
    esac
  done
  addon_lock
  if [[ "$remove" == 1 ]]; then
    [[ -n "$instance" ]] || { printf 'error: --remove needs --instance\n' >&2; exit 2; }
    cli remove-instance "$sid" "$instance" < /dev/null
    return
  fi
  if [[ -n "$instance" ]]; then
    out="$(cli configure "$sid" --instance "$instance")" || rc=$?
  else
    # addon_cli returns gate flags only while builtin.flag is on in .env, so
    # configure never switches on part of a disabled add-on.
    flags="$(env_flags_json)"
    out="$(cli configure "$sid" --flags "$flags")" || rc=$?
  fi
  [[ "$rc" == 0 ]] || exit "$rc"
  write_flags "$out"
}

cmd_enable() {
  local sid="${1:-}" plan entry name gate
  [[ -n "$sid" && $# -eq 1 ]] || usage_error "enable takes one short id"
  addon_lock
  cli_checked flags plan-enable "$sid"
  plan="$CLI_OUT"
  local services=()
  while IFS= read -r entry; do
    [[ -n "$entry" ]] || continue
    name="${entry%%@*}"
    gate=""
    [[ "$entry" == *@* ]] && gate="${entry#*@}"
    if [[ -z "$gate" ]] || gate_on "$plan" "$gate"; then
      services+=("$name")
    fi
  done <<EOF_SERVICES
$(json_list services "$plan")
EOF_SERVICES
  pull_images ${services[@]+"${services[@]}"}
  rollback_record "$plan"
  run_step flags "" apply_flags "$plan" \
    || ev_fail command_failed flags "writing the flags or syncing COMPOSE_PROFILES failed" "$EV_LAST_LOG"
  if [[ ${#services[@]} -gt 0 ]]; then
    run_step start "" docker compose up -d --wait "${services[@]}" \
      || ev_fail compose_failed start "docker compose up failed for: ${services[*]}" "$EV_LAST_LOG"
  fi
  run_step reload "" hermes_reload \
    || ev_fail compose_failed reload "make hermes-reload failed" "$EV_LAST_LOG"
  EV_ON_FAIL=""
  ev_done "$sid" enable "" ""
}

cmd_disable() {
  local sid="${1:-}" purge=0 plan line project vol
  [[ -n "$sid" ]] || usage_error "disable needs a short id"
  shift
  case "${1:-}" in
    "") ;;
    --purge) purge=1; shift ;;
    *) usage_error "disable: unknown option $1" ;;
  esac
  [[ $# -eq 0 ]] || usage_error "disable: too many arguments"
  addon_lock
  cli_checked flags plan-disable "$sid"
  plan="$CLI_OUT"
  run_step flags "" apply_flags "$plan" \
    || ev_fail command_failed flags "writing the flags or syncing COMPOSE_PROFILES failed" "$EV_LAST_LOG"
  local services=() volumes=()
  while IFS= read -r line; do
    [[ -n "$line" ]] && services+=("$line")
  done <<EOF_SERVICES
$(json_list services "$plan")
EOF_SERVICES
  if [[ ${#services[@]} -gt 0 ]]; then
    run_step stop "" docker compose stop "${services[@]}" \
      || ev_fail compose_failed stop "docker compose stop failed for: ${services[*]}" "$EV_LAST_LOG"
  fi
  if [[ "$purge" == 1 ]]; then
    while IFS= read -r line; do
      [[ -n "$line" ]] && volumes+=("$line")
    done <<EOF_VOLUMES
$(json_list volumes "$plan")
EOF_VOLUMES
    if [[ ${#volumes[@]} -gt 0 ]]; then
      # A stopped container still holds its volumes.
      run_step delete "" docker compose rm -f "${services[@]}" \
        || ev_fail compose_failed delete "docker compose rm failed for: ${services[*]}" "$EV_LAST_LOG"
      project="$(compose_project)"
      for vol in "${volumes[@]}"; do
        if docker volume inspect "${project}_${vol}" > /dev/null 2>&1; then
          run_step delete "$vol" docker volume rm "${project}_${vol}" \
            || ev_fail compose_failed delete "docker volume rm ${project}_${vol} failed" "$EV_LAST_LOG"
        fi
      done
    fi
    ev_step delete running "data/addons/${sid}"
    cli_checked delete purge "$sid"
    ev_step delete "done" "data/addons/${sid}"
  fi
  run_step reload "" hermes_reload \
    || ev_fail compose_failed reload "make hermes-reload failed" "$EV_LAST_LOG"
  ev_done "$sid" disable "" ""
}

cmd_run() {
  local sid="${1:-}" command="${2:-}" plan rc=0 line
  [[ -n "$sid" && -n "$command" ]] || usage_error "run needs a short id and a command"
  shift 2
  local planargs=(plan-run "$sid" "$command")
  case "${1:-}" in
    "") ;;
    --instance) [[ $# -eq 2 ]] || usage_error "--instance needs one slug"; planargs+=(--instance "$2"); shift 2 ;;
    *) usage_error "run: unknown option $1" ;;
  esac
  [[ $# -eq 0 ]] || usage_error "run: too many arguments"
  plan="$(cli "${planargs[@]}" < /dev/null)" || rc=$?
  [[ "$rc" == 0 ]] || exit "$rc"
  local argv=()
  while IFS= read -r line; do
    argv+=("$line")
  done <<EOF_ARGV
$plan
EOF_ARGV
  docker compose run --rm --no-deps -T "${argv[@]}" || exit 4
}

trap on_exit EXIT

sub="${1:-}"
[[ -n "$sub" ]] || usage_error "no command given"
shift
# --json may come anywhere among enable's and disable's options.
json=0
args=()
case "$sub" in
  enable | disable)
    for a in "$@"; do
      if [[ "$a" == --json ]]; then json=1; else args+=("$a"); fi
    done
    set -- ${args[@]+"${args[@]}"}
    ;;
esac
# list and run change nothing and keep the shell's default signal handling.
case "$sub" in
  configure | enable | disable) ev_init "$json" ;;
esac
case "$sub" in
  list) cmd_list "$@" ;;
  configure) cmd_configure "$@" ;;
  enable) cmd_enable "$@" ;;
  disable) cmd_disable "$@" ;;
  run) cmd_run "$@" ;;
  -h | --help | help) usage ;;
  *) printf 'error: unknown command %s\n' "$sub" >&2; exit 2 ;;
esac
