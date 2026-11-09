#!/usr/bin/env bash
# List, configure, enable, disable and run built-in add-ons (addons/<id>/addon.json).
# This is the interface Boundary calls; scripts/lib/addon_cli.py does the
# parsing and file work in the Hermes image (stack_python), so the host needs
# only bash and the container engine.
#
#   scripts/addon.sh list --json
#   scripts/addon.sh configure <short-id> [--instance <slug>] < values.json
#   scripts/addon.sh configure <short-id> --instance <slug> --remove
#   scripts/addon.sh enable <short-id>
#   scripts/addon.sh disable <short-id> [--purge]
#   scripts/addon.sh run <short-id> <command> [--instance <slug>]
#
# configure reads one JSON object on stdin. Secret values go only to
# data/addons/<short-id>/secrets; bool settings with a flag write that flag to
# .env. enable checks every required setting, sets builtin.flag=1 (and each
# gate flag a stored setting owns), syncs COMPOSE_PROFILES, starts the
# services whose gate is on and reloads Hermes. disable sets the flag and every
# gate flag to 0, syncs, stops the services and reloads; --purge also deletes
# data/addons/<short-id> and the compose volumes in services.*.volumes.
#
# Exit codes: 0 success, 2 invalid input, 3 a required setting is missing,
# 4 the command failed in its container.
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

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
# usage [CODE]: the synopsis from the header, on stderr unless CODE is 0.
usage() {
  if [[ "${1:-2}" == 0 ]]; then
    sed -n '7,12s/^# *//p' "${BASH_SOURCE[0]}"
  else
    sed -n '7,12s/^# *//p' "${BASH_SOURCE[0]}" >&2
  fi
  exit "${1:-2}"
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
  make hermes-reload || die "make hermes-reload failed"
}

cmd_list() {
  [[ "${1:-}" == "--json" && $# -eq 1 ]] || usage
  cli list --flags "$(env_flags_json)" < /dev/null
}

cmd_configure() {
  local sid="${1:-}" instance="" remove=0 out rc=0
  [[ -n "$sid" ]] || usage
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --instance) [[ $# -ge 2 ]] || usage; instance="$2"; shift 2 ;;
      --remove) remove=1; shift ;;
      *) usage ;;
    esac
  done
  if [[ "$remove" == 1 ]]; then
    [[ -n "$instance" ]] || { printf 'error: --remove needs --instance\n' >&2; exit 2; }
    cli remove-instance "$sid" "$instance" < /dev/null
    return
  fi
  if [[ -n "$instance" ]]; then
    out="$(cli configure "$sid" --instance "$instance")" || rc=$?
  else
    out="$(cli configure "$sid")" || rc=$?
  fi
  [[ "$rc" == 0 ]] || exit "$rc"
  write_flags "$out"
}

cmd_enable() {
  local sid="${1:-}" plan rc=0 entry name gate
  [[ -n "$sid" && $# -eq 1 ]] || usage
  plan="$(cli plan-enable "$sid" < /dev/null)" || rc=$?
  [[ "$rc" == 0 ]] || exit "$rc"
  write_flags "$plan"
  "${ROOT}/scripts/sync-profiles.sh"
  local services=()
  while IFS= read -r entry; do
    name="${entry%%@*}"
    gate=""
    [[ "$entry" == *@* ]] && gate="${entry#*@}"
    if [[ -z "$gate" ]] || truthy "$(env_get "$gate")"; then
      services+=("$name")
    fi
  done <<EOF_SERVICES
$(json_list services "$plan")
EOF_SERVICES
  if [[ ${#services[@]} -gt 0 ]]; then
    docker compose up -d --wait "${services[@]}" || die "docker compose up failed for: ${services[*]}"
  fi
  hermes_reload
}

cmd_disable() {
  local sid="${1:-}" purge=0 plan rc=0 line project vol
  [[ -n "$sid" ]] || usage
  shift
  case "${1:-}" in
    "") ;;
    --purge) purge=1; shift ;;
    *) usage ;;
  esac
  [[ $# -eq 0 ]] || usage
  plan="$(cli plan-disable "$sid" < /dev/null)" || rc=$?
  [[ "$rc" == 0 ]] || exit "$rc"
  write_flags "$plan"
  "${ROOT}/scripts/sync-profiles.sh"
  local services=() volumes=()
  while IFS= read -r line; do
    [[ -n "$line" ]] && services+=("$line")
  done <<EOF_SERVICES
$(json_list services "$plan")
EOF_SERVICES
  if [[ ${#services[@]} -gt 0 ]]; then
    docker compose stop "${services[@]}" || die "docker compose stop failed for: ${services[*]}"
  fi
  if [[ "$purge" == 1 ]]; then
    while IFS= read -r line; do
      [[ -n "$line" ]] && volumes+=("$line")
    done <<EOF_VOLUMES
$(json_list volumes "$plan")
EOF_VOLUMES
    if [[ ${#volumes[@]} -gt 0 ]]; then
      # A stopped container still holds its volumes.
      docker compose rm -f "${services[@]}" || die "docker compose rm failed for: ${services[*]}"
      project="$(compose_project)"
      for vol in "${volumes[@]}"; do
        if docker volume inspect "${project}_${vol}" > /dev/null 2>&1; then
          docker volume rm "${project}_${vol}" || die "docker volume rm ${project}_${vol} failed"
        fi
      done
    fi
    cli purge "$sid" < /dev/null
  fi
  hermes_reload
}

cmd_run() {
  local sid="${1:-}" command="${2:-}" plan rc=0 line
  [[ -n "$sid" && -n "$command" ]] || usage
  shift 2
  local planargs=(plan-run "$sid" "$command")
  case "${1:-}" in
    "") ;;
    --instance) [[ $# -eq 2 ]] || usage; planargs+=(--instance "$2"); shift 2 ;;
    *) usage ;;
  esac
  [[ $# -eq 0 ]] || usage
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

sub="${1:-}"
[[ -n "$sub" ]] || usage
shift
case "$sub" in
  list) cmd_list "$@" ;;
  configure) cmd_configure "$@" ;;
  enable) cmd_enable "$@" ;;
  disable) cmd_disable "$@" ;;
  run) cmd_run "$@" ;;
  -h | --help | help) usage 0 ;;
  *) printf 'error: unknown command %s\n' "$sub" >&2; exit 2 ;;
esac
