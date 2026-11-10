#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# List, configure, enable, disable, connect, disconnect and run built-in add-ons
# (addons/<id>/addon.json).
# This is the interface Boundary calls; scripts/lib/addon_cli.py does the
# parsing and file work in the Hermes image (stack_python), so the host needs
# only bash and the container engine.
#
#   scripts/addon.sh list --json [--sizes]
#   scripts/addon.sh configure <short-id> [--instance <slug>] < values.json
#   scripts/addon.sh configure <short-id> --instance <slug> --remove
#   scripts/addon.sh enable <short-id> [--reinit] [--json]
#   scripts/addon.sh disable <short-id> [--purge] [--json]
#   scripts/addon.sh connect <short-id> --instance <slug> [--json] < values.json
#   scripts/addon.sh disconnect <short-id> --instance <slug> [--json]
#   scripts/addon.sh remove <short-id> --delete-data [--json]
#   scripts/addon.sh run <short-id> <command> [--instance <slug>]
#
# configure reads one JSON object on stdin. Secret values go only to
# data/addons/<short-id>/secrets; bool settings with a flag write that flag to
# .env while the add-on is enabled. enable checks every required setting, sets builtin.flag=1 (and each
# gate flag a stored setting owns), syncs COMPOSE_PROFILES, starts the
# services whose gate is on, runs the manifest's init hook once (--reinit runs
# it again; data/addons/<short-id>/state.json records it) and reloads Hermes. disable sets the flag and every
# gate flag to 0, syncs, stops the services and reloads; --purge then runs
# remove --delete-data's work in the same lock, and prints one done line.
#
# remove --delete-data refuses (bad_input) while the add-on is enabled. It
# runs `docker compose rm -f -s` on every service and one-shot, removes each
# compose volume in data.volumes that exists, and deletes each data.paths
# directory and data/addons/<short-id>, all as step delete. A data path is its
# env from the caller's environment, then .env, else its default, made
# absolute against the repo root without resolving symlinks. It is deleted
# only when it lies inside ./data or ASSISTANT_DATA_ROOT, isn't one of them,
# doesn't hold one, isn't, doesn't hold and doesn't lie in hermes, addons or a
# USER_DATA_SUBDIRS directory under either, has no symlink between
# the data directory and itself, and no other enabled add-on declares it or a
# path that overlaps it. Otherwise a `delete` line with "kept" and "reason"
# reports it and it stays. A symlink is unlinked, never followed.
#
# Reloading Hermes (step reload, run by enable, disable, connect and
# disconnect) runs both bootstraps with HERMES_BOOTSTRAP_FORCE=1 and then
# scripts/vault.sh reload-plugins. It never recreates hermes. A manifest with
# onToggle gets step recreate on enable and disable when the builtin flag
# changed value: `up -d --no-deps --force-recreate` for onToggle.recreate, then
# onToggle.script, before the reload; the done line lists those services in
# restart. disable also stops each `uses` service that addon_cli.py maps to a
# compose service when `docker compose config --services` no longer lists it.
#
# connect stages the values on stdin in data/addons/.staging.XXXXXX (mode
# 0600, removed on exit) and checks them against instances.settings. When the
# manifest has instances.connect, step check runs that hook with
# --slug <slug> --json and the values on stdin; it checks the credential and
# stores what it keeps. Step register then writes the instance as configure
# --instance does, and step reload reloads Hermes. The done line carries the
# hook's account and mcp names. disconnect runs instances.disconnect (stdin
# /dev/null) as step check, removes the instance (register) and reloads. A
# hook's output is redacted before it is logged or relayed: each secret value
# from the staged values becomes [redacted]. An existing slug on connect is
# exists, an unknown one on disconnect not_found, both exit 2.
#
# --json prints one JSON object per line on stdout (scripts/lib/addon-events.sh):
# `pull` lines (enable only: it pulls the images first), `setup` lines per
# step, then one `done` or `error` line. Step output goes to stderr without
# --json.
#
# configure, enable, disable, connect, disconnect and remove hold data/addons/.lock
# (a symlink to their PID) while they run; a second call fails with busy. A
# failed or interrupted enable writes the flags back as they were, syncs
# COMPOSE_PROFILES and stops the services if the add-on was off, and its
# error line has "rolledBack":true.
#
# Exit codes: 0 success, 1 compose_failed, busy or an invalid data root
# (command_failed), 2 bad_input (invalid input or usage) and connect's exists
# and disconnect's not_found, 3 missing_settings, 4 command_failed (run:
# docker compose run failed; addon_cli.py failed; SIGINT or SIGTERM; an
# unexpected exit) and every error a hook reports.
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
usage() { sed -n '9,17s/^# *//p' "${BASH_SOURCE[0]}"; }
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
# Checked below, once --json is known.
DATA_ROOT="$(resolve_data_root_path)"

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

# reload_hermes: the two bootstraps with HERMES_BOOTSTRAP_FORCE=1, then
# vault.sh reload-plugins, which tells Hermes to reload the profiles the
# bootstrap marked. It does not recreate hermes. Returns 1 when a bootstrap
# fails and 2 when reload-plugins fails.
reload_hermes() {
  docker compose run --rm -e HERMES_BOOTSTRAP_FORCE=1 hermes-api-bootstrap || return 1
  docker compose run --rm --no-deps -e HERMES_BOOTSTRAP_FORCE=1 hermes-browser-bootstrap || return 1
  ./scripts/vault.sh reload-plugins || return 2
}
# reload_step: reload_hermes as step reload. A bootstrap failure is
# compose_failed, a reload-plugins failure command_failed.
reload_step() {
  local rc=0
  run_step reload "" reload_hermes || rc=$?
  case "$rc" in
    0) ;;
    2) ev_fail command_failed reload "vault.sh reload-plugins failed" "$EV_LAST_LOG" ;;
    *) ev_fail compose_failed reload "a Hermes bootstrap failed" "$EV_LAST_LOG" ;;
  esac
}

# plan_builtin PLAN: the builtin flag name in a plan.
plan_builtin() {
  printf '%s' "$1" | sed -n 's/.*"builtin":"\([A-Z][A-Z0-9_]*\)".*/\1/p'
}

# toggle_recreate PLAN SID: for enable and disable, when onToggle names services
# and the builtin flag changed value in this run (CHANGED is 1): step recreate
# runs `up -d --no-deps --force-recreate` for them, then onToggle.script from
# ROOT. Sets RECREATED (the services, comma-separated).
RECREATED=""
toggle_recreate() {
  local plan="$1" sid="$2" names=() name line script=()
  RECREATED=""
  [[ "$CHANGED" == 1 ]] || return 0
  while IFS= read -r name; do
    [[ -n "$name" ]] && names+=("$name")
  done <<EOF_RECREATE
$(json_list recreate "$plan")
EOF_RECREATE
  [[ ${#names[@]} -gt 0 ]] || return 0
  # Resolve the script first: a rejected argv must not come after the recreate.
  cli_checked recreate toggle-script "$sid"
  if [[ -n "$CLI_OUT" ]]; then
    while IFS= read -r line; do script+=("$line"); done <<EOF_SCRIPT
$CLI_OUT
EOF_SCRIPT
  fi
  run_step recreate "" docker compose up -d --no-deps --force-recreate "${names[@]}" \
    || ev_fail compose_failed recreate "docker compose up --force-recreate failed for: ${names[*]}" "$EV_LAST_LOG"
  if [[ ${#script[@]} -gt 0 ]]; then
    run_step recreate "" "${script[@]}" \
      || ev_fail command_failed recreate "${script[0]} failed" "$EV_LAST_LOG"
  fi
  RECREATED="$(IFS=,; printf '%s' "${names[*]}")"
}

# cli_checked STEP ARGS...: cli with stdin from $CLI_IN (default /dev/null) and its stdout in
# CLI_OUT. On failure, ev_fail with cli's own error line as the message:
# exit 2 is bad_input, 3 missing_settings, anything else command_failed.
# An empty STEP gives "step":null.
CLI_OUT=""
CLI_OPEN_STEP=""
cli_checked() {
  local step="$1" err rc=0 msg code
  shift
  err="$(ev_log cli)"
  CLI_OUT="$(cli "$@" < "${CLI_IN:-/dev/null}" 2> "$err")" || rc=$?
  [[ "$rc" == 0 ]] && return 0
  msg="$(sed -n 's/^error: //p' "$err" | tail -n 1)"
  [[ -n "$msg" ]] || msg="addon_cli.py $1 failed (exit ${rc})"
  case "$rc" in
    2) code=bad_input ;;
    3) code=missing_settings ;;
    *) code=command_failed ;;
  esac
  if [[ -n "$CLI_OPEN_STEP" ]]; then
    ev_step "$CLI_OPEN_STEP" error
    CLI_OPEN_STEP=""
  fi
  ev_fail "$code" "$step" "$msg" "$err"
}
# cli_step STEP ARGS...: cli_checked as a step of its own, between running and
# done (or error) lines.
cli_step() {
  local step="$1"
  shift
  ev_step "$step" running
  CLI_OPEN_STEP="$step"
  cli_checked "$step" "$@"
  CLI_OPEN_STEP=""
  ev_step "$step" "done"
}

# The staged values of connect. stage_stdin copies stdin to a 0600 file under
# data/addons, which the Hermes container mounts, so addon_cli.py can read it
# by path; on_exit removes it.
STAGED=""
stage_stdin() {
  local old
  old="$(umask)"
  umask 077
  STAGED="$(mktemp "${DATA_ROOT}/addons/.staging.XXXXXX")" || STAGED=""
  umask "$old"
  [[ -n "$STAGED" ]] || ev_fail command_failed "" "could not create a staging file in ${DATA_ROOT}/addons" ""
  cat > "$STAGED" || ev_fail command_failed "" "reading the values on stdin failed" ""
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
    clear_staging
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
  clear_staging
}
# clear_staging: deletes data/addons/.staging.* left by a connect that was
# killed before its EXIT trap ran. They hold plaintext values. Only the lock
# holder calls it, so no other run owns one.
clear_staging() {
  rm -f "${DATA_ROOT}/addons"/.staging.*
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
# on_exit: the EXIT trap. A run that exits non-zero without an error line (a
# `set -e` exit) while enable's rollback is armed, or in JSON mode, gets one
# here: the rollback runs and the error is command_failed (exit 4).
on_exit() {
  local rc=$? fail=0
  if [[ "$rc" != 0 && "$EV_ERRORED" != 1 && ( -n "$EV_ON_FAIL" || "$EV_JSON" == 1 ) ]]; then
    fail=1
    ev_error command_failed "$EV_STEP" "addon.sh stopped unexpectedly (exit ${rc})" "$EV_CUR_LOG"
  fi
  if [[ -n "$STAGED" ]]; then
    rm -f "$STAGED"
  fi
  if [[ -n "$SIZES_DIR" ]]; then
    rm -rf "$SIZES_DIR"
  fi
  addon_unlock
  ev_cleanup
  if [[ "$fail" == 1 ]]; then
    exit "$(ev_exit_status command_failed)"
  fi
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
  ROLLBACK_SERVICES="$(json_list services "$plan" | sed 's/@.*//')"$'\n'"$(json_list oneshots "$plan" | sed 's/@.*//')"
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

# Hooks. An init hook that succeeds is recorded (mark-init) inside its step.
# run_hook STEP SID HOOK [plan-hook options]: runs the manifest's hook as
# step STEP. Sets HOOK_ACCOUNT (the `account`
# its `done` line reported, else empty), HOOK_MCP (its `mcp` names,
# comma-separated) and HOOK_MESSAGE (its `message`, cleaned, else empty). The hook's stdin is HOOK_STDIN. Events and the result
# come from the hook's stdout only; its stderr goes to the step log and is
# never parsed. With HOOK_REDACT set (a staged values file), stdout and stderr
# each go through `cli redact` before anything stores or prints them. A hook that fails goes through
# ev_fail, so the caller's rollback runs: its error line's name passes through
# when it is a shared name or in instances.errors, any other name becomes
# command_failed (addon_cli.py hook-result decides). In JSON mode the hook's
# `setup` and `pull` lines are printed inside the step with "step" set and
# their strings cleaned; everything else goes to the step log.
HOOK_ACCOUNT=""
HOOK_MCP=""
HOOK_MESSAGE=""
HOOK_STDIN=/dev/null
HOOK_REDACT=""
# hook_exec RAW SID HOOK KIND STEP ARGV...: KIND is `script` (run from ROOT) or
# a compose service. The hook's stdout is copied to RAW and filtered by
# hook-events; its stderr goes to this function's stderr (the step log). The step fails, so it never shows `done` first, when the hook
# exits non-zero, when it printed an `error` line (even with exit 0), or when
# recording init fails. Host files are never passed to cli: RAW goes on stdin.
hook_exec() {
  local raw="$1" sid="$2" hook="$3" kind="$4" step="$5" st code
  shift 5
  set +e
  hook_split "$sid" "$kind" "$@" 5>&1 | hook_filter "$sid" | tee "$raw" | hook_events "$step"
  st=("${PIPESTATUS[@]}")
  set -e
  [[ "${st[1]}" == 0 && "${st[2]}" == 0 && "${st[3]}" == 0 ]] || return 1
  [[ "${st[0]}" == 0 ]] || return "${st[0]}"
  code="$(cli hook-result "$sid" 0 < "$raw" 2> /dev/null | sed -n 1p)" || return 1
  [[ -z "$code" ]] || return 1
  if [[ "$hook" == init ]] && ! cli mark-init "$sid" < /dev/null; then
    printf '{"phase":"error","error":"command_failed","message":"recording init in state.json failed"}\n' >> "$raw"
    return 1
  fi
}
# hook_split SID KIND ARGV...: the hook with its stdout on fd 5 and its stderr
# through hook_filter to stderr. Returns the hook's status, or 1 when the
# stderr filter failed.
hook_split() {
  local sid="$1" st
  shift
  set +e
  hook_cmd "$@" 2>&1 1>&5 5>&- | hook_filter "$sid" >&2 5>&-
  st=("${PIPESTATUS[@]}")
  set -e
  [[ "${st[1]}" == 0 ]] || return 1
  return "${st[0]}"
}
# hook_filter SID: `cli redact` with HOOK_REDACT, else cat.
hook_filter() {
  if [[ -n "$HOOK_REDACT" ]]; then cli redact "$1" "$HOOK_REDACT"; else cat; fi
}
# hook_cmd KIND ARGV...: the hook itself, stdin from HOOK_STDIN.
hook_cmd() {
  local kind="$1"
  shift
  if [[ "$kind" == script ]]; then
    "$@" < "$HOOK_STDIN"
  else
    docker compose run --rm --no-deps -T "$kind" "$@" < "$HOOK_STDIN"
  fi
}
hook_events() {
  if [[ "$EV_JSON" == 1 ]]; then cli hook-events "$1" >&3; else cli hook-events "$1"; fi
}
run_hook() {
  local step="$1" sid="$2" hook="$3" raw rc=0 line first="" kind="" argv=() out code msg
  shift 3
  HOOK_ACCOUNT=""
  HOOK_MCP=""
  HOOK_MESSAGE=""
  cli_checked "$step" plan-hook "$sid" "$hook" "$@"
  [[ -n "$CLI_OUT" ]] || return 0
  while IFS= read -r line; do
    if [[ -z "$first" ]]; then first="$line"; else argv+=("$line"); fi
  done <<EOF_HOOK
$CLI_OUT
EOF_HOOK
  if [[ "$first" == script ]]; then kind=script; else kind="${first#service }"; fi
  raw="$(ev_log hook)"
  run_step "$step" "" hook_exec "$raw" "$sid" "$hook" "$kind" "$step" "${argv[@]}" || rc=$?
  CLI_IN="$raw" cli_checked "$step" hook-result "$sid" "$rc"
  out="$CLI_OUT"
  code="$(printf '%s\n' "$out" | sed -n 1p)"
  msg="$(printf '%s\n' "$out" | sed -n 2p)"
  HOOK_ACCOUNT="$(printf '%s\n' "$out" | sed -n 3p)"
  HOOK_MCP="$(printf '%s\n' "$out" | sed -n 4p)"
  HOOK_MESSAGE="$(printf '%s\n' "$out" | sed -n 5p)"
  [[ -z "$code" ]] || ev_fail "$code" "$step" "$msg" "$EV_LAST_LOG"
}

# cmd_list --json [--sizes]: the catalog. Image and memory figures come from
# `docker compose --profile '*' config`, which reads every service and starts
# nothing; when it fails the catalog prints without them and one warning goes
# to stderr. --sizes also asks the registry for each image's manifest
# (docker manifest inspect --verbose) to add downloadSize. list takes no lock.
# SIZES_DIR: list --sizes's scratch directory; on_exit removes it.
SIZES_DIR=""
cmd_list() {
  local sizes=0 json=0 config="" platform="" dir="" images image n=0 args=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) json=1; shift ;;
      --sizes) sizes=1; shift ;;
      *) usage_error "list takes --json and --sizes only" ;;
    esac
  done
  [[ "$json" == 1 ]] || usage_error "list takes --json and --sizes only"
  if ! config="$(docker compose --profile '*' config --format json 2> /dev/null)"; then
    config=""
    printf 'warning: docker compose config failed; listing without image and memory figures\n' >&2
  fi
  if [[ "$sizes" == 1 && -n "$config" ]]; then
    # Under the data root so the Hermes container, which mounts only the repo,
    # the data root and the add-ons dir, can read it.
    mkdir -p "${DATA_ROOT}/addons"
    SIZES_DIR="$(mktemp -d "${DATA_ROOT}/addons/.sizes.XXXXXX")" || SIZES_DIR=""
    dir="$SIZES_DIR"
  fi
  if [[ -n "$dir" ]]; then
    platform="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2> /dev/null)" || platform=""
    if [[ -n "$platform" ]]; then
      images="$(printf '%s' "$config" | cli images 2> /dev/null)" || images=""
      while IFS= read -r image; do
        [[ -n "$image" ]] || continue
        n=$((n + 1))
        printf '%s\n' "$image" > "${dir}/${n}.image"
        docker manifest inspect --verbose "$image" > "${dir}/${n}.json" 2> /dev/null || rm -f "${dir}/${n}.image" "${dir}/${n}.json"
      done <<< "$images"
      args=(--sizes-dir "$dir" --platform "$platform")
    else
      printf 'warning: docker version failed; listing without download sizes\n' >&2
    fi
  fi
  printf '%s' "$config" | cli list --flags "$(env_flags_json)" ${args[@]+"${args[@]}"}
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

# start_failed SERVICES...: after `up -d --wait` fails, finds the container
# that exited non-zero (SERVICES in the order given: one-shots, then the
# rest). Docker blames the service that depends on a failed one-shot, so
# `up`'s own output often names the wrong one. Without such a container the
# error keeps the `up` output as logTail and has no target.
start_failed() {
  local up_log="$EV_LAST_LOG" csv="" svc bad="" log
  for svc in "$@"; do csv="${csv:+${csv},}${svc}"; done
  bad="$(docker compose ps -a --format json "$@" 2> /dev/null | cli failed-container "$csv" 2> /dev/null)" || bad=""
  if [[ -n "$bad" ]]; then
    log="$(ev_log logs)"
    docker compose logs --no-color --tail 20 "$bad" > "$log" 2>&1 || true
    EV_TARGET="$bad"
    ev_fail compose_failed start "docker compose up failed: $bad exited with an error" "$log"
  fi
  ev_fail compose_failed start "docker compose up failed for: $*" "$up_log"
}

# gated_names PLAN KEY [off]: the compose names under KEY ("services",
# "oneshots", "mcp" or "stop") whose gate is on or that have no gate, one per
# line. With "off", the names whose gate is off.
gated_names() {
  local entry gate
  while IFS= read -r entry; do
    [[ -n "$entry" ]] || continue
    gate=""
    [[ "$entry" == *@* ]] && gate="${entry#*@}"
    if [[ "${3:-}" == off ]]; then
      if [[ -n "$gate" ]] && ! gate_on "$1" "$gate"; then
        printf '%s\n' "${entry%%@*}"
      fi
    elif [[ -z "$gate" ]] || gate_on "$1" "$gate"; then
      printf '%s\n' "${entry%%@*}"
    fi
  done <<EOF_ENTRIES
$(json_list "$2" "$1")
EOF_ENTRIES
}

# check_hermes_user PLAN: on Linux, the services that mount add-on directories
# run as HERMES_UID:HERMES_GID, so those must be the caller's ids or the
# mounts are unreadable. Runs before any flag is written.
check_hermes_user() {
  local kind flag want have
  [[ -n "$(json_list mounts "$1")" ]] || return 0
  [[ "$(uname -s)" == Linux ]] || return 0
  for kind in UID GID; do
    flag=-u
    [[ "$kind" == UID ]] || flag=-g
    want="$(env_get "HERMES_${kind}" 1000)"
    have="$(id "$flag")"
    [[ "$want" == "$have" ]] \
      || ev_fail bad_input "" "HERMES_${kind} ${want} is not your user id ${have}; set HERMES_UID and HERMES_GID in .env" ""
  done
}

cmd_enable() {
  local sid="${1:-}" plan name mcp reinit=()
  [[ -n "$sid" ]] || usage_error "enable takes one short id"
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --reinit) reinit=(--reinit); shift ;;
      *) usage_error "enable: unknown option $1" ;;
    esac
  done
  addon_lock
  cli_checked flags plan-enable "$sid"
  plan="$CLI_OUT"
  check_hermes_user "$plan"
  local services=() oneshots=() stops=() list
  list="$(gated_names "$plan" services)"
  [[ -z "$list" ]] || while IFS= read -r name; do services+=("$name"); done <<EOF_SERVICES
$list
EOF_SERVICES
  list="$(gated_names "$plan" oneshots)"
  [[ -z "$list" ]] || while IFS= read -r name; do oneshots+=("$name"); done <<EOF_ONESHOTS
$list
EOF_ONESHOTS
  list="$(gated_names "$plan" stop off)"
  [[ -z "$list" ]] || while IFS= read -r name; do stops+=("$name"); done <<EOF_STOPS
$list
EOF_STOPS
  pull_images ${services[@]+"${services[@]}"} ${oneshots[@]+"${oneshots[@]}"}
  rollback_record "$plan"
  CHANGED=0
  truthy "$(env_get "$(plan_builtin "$plan")")" || CHANGED=1
  run_step flags "" apply_flags "$plan" \
    || ev_fail command_failed flags "writing the flags or syncing COMPOSE_PROFILES failed" "$EV_LAST_LOG"
  # One-shots run to completion first: a service may depend on one.
  for name in ${oneshots[@]+"${oneshots[@]}"}; do
    run_step start "$name" docker compose up --no-deps --exit-code-from "$name" "$name" \
      || { EV_TARGET="$name"; ev_fail compose_failed start "one-shot service $name failed" "$EV_LAST_LOG"; }
  done
  if [[ ${#services[@]} -gt 0 ]]; then
    run_step start "" docker compose up -d --wait "${services[@]}" \
      || start_failed ${oneshots[@]+"${oneshots[@]}"} "${services[@]}"
  fi
  if [[ ${#stops[@]} -gt 0 ]]; then
    run_step stop "" docker compose stop "${stops[@]}" \
      || ev_fail compose_failed stop "docker compose stop failed for: ${stops[*]}" "$EV_LAST_LOG"
  fi
  run_hook init "$sid" init ${reinit[@]+"${reinit[@]}"}
  toggle_recreate "$plan" "$sid"
  reload_step
  EV_ON_FAIL=""
  # The manifest's gated names, then the init hook's, each once.
  mcp="$({ gated_names "$plan" mcp; printf '%s\n' "$HOOK_MCP" | tr ',' '\n'; } | awk 'NF && !seen[$0]++' | paste -sd, -)"
  ev_done "$sid" enable "$RECREATED" "$mcp" "$HOOK_ACCOUNT" "$HOOK_MESSAGE"
}

# stop_unused_uses PLAN: stops each compose service the manifest's `uses` maps
# to (step stop, with that target) unless `docker compose config --services`,
# which applies the active profiles, still lists it. A service no profile gates
# is always listed, so it is never stopped.
stop_unused_uses() {
  local names=() name active="" cfg_log
  while IFS= read -r name; do
    [[ -n "$name" ]] && names+=("$name")
  done <<EOF_USES
$(json_list uses "$1")
EOF_USES
  [[ ${#names[@]} -gt 0 ]] || return 0
  cfg_log="$(ev_log config)"
  active="$(docker compose config --services 2> "$cfg_log")" \
    || ev_fail compose_failed stop "docker compose config --services failed" "$cfg_log"
  for name in "${names[@]}"; do
    if ! printf '%s\n' "$active" | grep -qx -- "$name"; then
      run_step stop "$name" docker compose stop "$name" \
        || { EV_TARGET="$name"; ev_fail compose_failed stop "docker compose stop failed for: $name" "$EV_LAST_LOG"; }
    fi
  done
}

cmd_disable() {
  local sid="${1:-}" purge=0 plan line
  [[ -n "$sid" ]] || usage_error "disable needs a short id"
  shift
  case "${1:-}" in
    "") ;;
    --purge) purge=1; shift ;;
    *) usage_error "disable: unknown option $1" ;;
  esac
  [[ $# -eq 0 ]] || usage_error "disable: too many arguments"
  addon_lock
  cli_checked flags plan-disable "$sid" --flags "$(env_flags_json)"
  plan="$CLI_OUT"
  CHANGED=0
  ! truthy "$(env_get "$(plan_builtin "$plan")")" || CHANGED=1
  run_step flags "" apply_flags "$plan" \
    || ev_fail command_failed flags "writing the flags or syncing COMPOSE_PROFILES failed" "$EV_LAST_LOG"
  local services=()
  while IFS= read -r line; do
    [[ -n "$line" ]] && services+=("$line")
  done <<EOF_SERVICES
$(json_list services "$plan")
EOF_SERVICES
  if [[ ${#services[@]} -gt 0 ]]; then
    run_step stop "" docker compose stop "${services[@]}" \
      || ev_fail compose_failed stop "docker compose stop failed for: ${services[*]}" "$EV_LAST_LOG"
  fi
  stop_unused_uses "$plan"
  toggle_recreate "$plan" "$sid"
  reload_step
  [[ "$purge" == 1 ]] && remove_data "$sid"
  ev_done "$sid" disable "$RECREATED" ""
}

# data_env_json SID: sets DATA_ENV_JSON to {"NAME":"VALUE",...} for the env
# names `data-envs` gives (those data.paths declare, and nothing else from
# .env), each from the caller's exported environment, then .env, with a
# leading ~ expanded to $HOME. Empty values are left out, as compose's
# ${NAME:-default} ignores them. Only backslash and quote are escaped: a
# control character makes the JSON invalid, and plan-remove refuses it.
DATA_ENV_JSON=""
data_env_json() {
  local name value out=""
  cli_checked "" data-envs "$1"
  while IFS= read -r name; do
    [[ "$name" =~ ^[A-Z][A-Z0-9_]*$ ]] || continue
    # The exported environment only: addon.sh's own variables don't count.
    value="$(printenv "$name" || true)"
    [[ -n "$value" ]] || value="$(env_get "$name")"
    [[ -n "$value" ]] || continue
    # Compose expands a leading ~ to the home directory.
    # shellcheck disable=SC2088 # a literal ~ in the value
    if [[ "$value" == "~" ]]; then
      value="$HOME"
    elif [[ "$value" == "~/"* ]]; then
      value="${HOME}/${value#\~/}"
    fi
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    out="${out:+${out},}\"${name}\":\"${value}\""
  done <<EOF_NAMES
$CLI_OUT
EOF_NAMES
  DATA_ENV_JSON="{${out}}"
}

# ev_kept PATH REASON: the delete line for a data path that stays.
ev_kept() {
  if [[ "$EV_JSON" == 1 ]]; then
    printf '{"phase":"setup","step":"delete","status":"done","kept":%s,"reason":%s}\n' \
      "$(json_str "$1")" "$(json_str "$2")" >&3
  else
    printf 'kept %s: %s\n' "$1" "$2" >&4
  fi
}

# remove_data SID: remove --delete-data's work, under the caller's lock. Paths
# go from plan-remove to delete-path one per line: plan-remove refuses a path
# with a control character.
remove_data() {
  local sid="$1" line services=() volumes=() paths=() project vol path msg
  data_env_json "$sid"
  cli_checked "" plan-remove "$sid" --env "$DATA_ENV_JSON" --flags "$(env_flags_json)" --repo "$ROOT" --lines
  while IFS= read -r line; do
    case "$line" in
      "service "*) services+=("${line#service }") ;;
      "volume "*) volumes+=("${line#volume }") ;;
      "delete "*) paths+=("${line#delete }") ;;
      "kept "*) line="${line#kept }"; ev_kept "${line#*$'\t'}" "${line%%$'\t'*}" ;;
    esac
  done <<EOF_PLAN
$CLI_OUT
EOF_PLAN
  # A stopped container still holds its volumes and bind mounts.
  if [[ ${#services[@]} -gt 0 ]]; then
    run_step delete "" docker compose rm -f -s "${services[@]}" \
      || ev_fail compose_failed delete "docker compose rm failed for: ${services[*]}" "$EV_LAST_LOG"
  fi
  project="$(compose_project)"
  for vol in ${volumes[@]+"${volumes[@]}"}; do
    if docker volume inspect "${project}_${vol}" > /dev/null 2>&1; then
      run_step delete "$vol" docker volume rm "${project}_${vol}" \
        || { EV_TARGET="$vol"; ev_fail compose_failed delete "docker volume rm ${project}_${vol} failed" "$EV_LAST_LOG"; }
    fi
  done
  for path in ${paths[@]+"${paths[@]}"}; do
    if ! run_step delete "$path" cli delete-path "$path" --repo "$ROOT" --sid "$sid" < /dev/null; then
      msg=""
      [[ -n "$EV_LAST_LOG" ]] && msg="$(sed -n 's/^error: //p' "$EV_LAST_LOG" | tail -n 1)"
      EV_TARGET="$path"
      ev_fail command_failed delete "deleting ${path} failed${msg:+: ${msg}}" "$EV_LAST_LOG"
    fi
  done
}

cmd_remove() {
  local sid="${1:-}" delete=0
  [[ -n "$sid" ]] || usage_error "remove needs a short id"
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --delete-data) delete=1; shift ;;
      *) usage_error "remove: unknown option $1" ;;
    esac
  done
  [[ "$delete" == 1 ]] || usage_error "remove needs --delete-data"
  addon_lock
  remove_data "$sid"
  ev_done "$sid" remove "" ""
}

# instance_args CMD ARGS...: the slug from `--instance SLUG`, the only option
# connect and disconnect take, in INSTANCE_SLUG.
INSTANCE_SLUG=""
instance_args() {
  local cmd="$1"
  shift
  INSTANCE_SLUG=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --instance) [[ $# -ge 2 && -n "$2" ]] || usage_error "--instance needs a slug"; INSTANCE_SLUG="$2"; shift 2 ;;
      *) usage_error "${cmd}: unknown option $1" ;;
    esac
  done
  [[ -n "$INSTANCE_SLUG" ]] || usage_error "${cmd} needs --instance <slug>"
}

cmd_connect() {
  local sid="${1:-}" slug
  [[ -n "$sid" ]] || usage_error "connect needs a short id"
  shift
  instance_args connect "$@"
  slug="$INSTANCE_SLUG"
  addon_lock
  stage_stdin
  EV_REDACT="$STAGED"
  EV_REDACT_SID="$sid"
  CLI_IN="$STAGED" cli_checked "" check-instance "$sid" "$slug"
  if [[ "$CLI_OUT" != new ]]; then
    EV_EXIT=2
    ev_fail exists "" "${sid} already has an instance ${slug}" ""
  fi
  HOOK_STDIN="$STAGED"
  HOOK_REDACT="$STAGED"
  run_hook check "$sid" connect --slug "$slug"
  HOOK_STDIN=/dev/null
  HOOK_REDACT=""
  CLI_IN="$STAGED" cli_step register configure "$sid" --instance "$slug"
  reload_step
  ev_done "$sid" connect "" "$HOOK_MCP" "$HOOK_ACCOUNT" "$HOOK_MESSAGE"
}

cmd_disconnect() {
  local sid="${1:-}" slug
  [[ -n "$sid" ]] || usage_error "disconnect needs a short id"
  shift
  instance_args disconnect "$@"
  slug="$INSTANCE_SLUG"
  addon_lock
  cli_checked "" check-instance "$sid" "$slug" --slug-only
  if [[ "$CLI_OUT" != exists ]]; then
    EV_EXIT=2
    ev_fail not_found "" "${sid} has no instance ${slug}" ""
  fi
  run_hook check "$sid" disconnect --slug "$slug"
  cli_step register remove-instance "$sid" "$slug"
  reload_step
  ev_done "$sid" disconnect "" "" "" "$HOOK_MESSAGE"
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
# --json may come anywhere among the options of the commands that take it.
json=0
args=()
case "$sub" in
  enable | disable | connect | disconnect | remove)
    for a in "$@"; do
      if [[ "$a" == --json ]]; then json=1; else args+=("$a"); fi
    done
    set -- ${args[@]+"${args[@]}"}
    ;;
esac
# list and run change nothing and keep the shell's default signal handling.
case "$sub" in
  configure | enable | disable | connect | disconnect | remove) ev_init "$json" ;;
esac
# An invalid data root is the stack's configuration, not the call's input:
# command_failed, keeping the exit 1 it always had.
if ! msg="$(validate_data_root_path "$DATA_ROOT" 2>&1)"; then
  case "$sub" in
    configure | enable | disable | connect | disconnect | remove) EV_EXIT=1; ev_fail command_failed "" "$msg" "" ;;
    *) printf '%s\n' "$msg" >&2; exit 1 ;;
  esac
fi
case "$sub" in
  list) cmd_list "$@" ;;
  configure) cmd_configure "$@" ;;
  enable) cmd_enable "$@" ;;
  disable) cmd_disable "$@" ;;
  connect) cmd_connect "$@" ;;
  disconnect) cmd_disconnect "$@" ;;
  remove) cmd_remove "$@" ;;
  run) cmd_run "$@" ;;
  -h | --help | help) usage ;;
  *) printf 'error: unknown command %s\n' "$sub" >&2; exit 2 ;;
esac
