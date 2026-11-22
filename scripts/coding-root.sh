#!/usr/bin/env bash
# Manage shared coding roots for Hermes + OpenCode.
#
# Primary root: OPENCODE_WORKSPACE_HOST (mounted on the terminal backend and opencode).
# Extra roots: colon-prefixed entries in CODING_EXTRA_ROOTS (.env) plus matching
# bind mounts under the terminal service (hermes-worker, or hermes when
# HERMES_TERMINAL_BACKEND=local) and services.opencode in
# docker-compose.override.yml (gitignored).
#
# A synced root (CR 0046) is an extra root whose folder the stack creates at
# <data root>/coding-roots/<ID>/ and binds at a container path the client
# names, for a client that keeps a copy of one of its own folders on this
# host. CODING_EXTRA_ROOTS holds its container path like any other root; the
# registry beside the folders (scripts/lib/coding_roots.py) holds the host
# folder.
#
#   coding-root.sh list [--json] [--no-size]
#   coding-root.sh add DIR
#   coding-root.sh add --container-path PATH --id ID [--json]
#   coding-root.sh remove DIR
#   coding-root.sh remove --id ID [--delete-folder] [--json]
#
# add and remove hold the stack's config lock (scripts/lib/stack-lock.sh)
# while they change .env, the override and the registry. Neither recreates
# anything: they print the services to recreate.
#
# --json prints one line in addon.sh's shapes (scripts/lib/json-lines.sh):
#   add     {"phase":"done","id":ID,"hostPath":H,"containerPath":P,
#            "created":true|false,"recreate":[...]}
#   remove  {"phase":"done","id":ID,"removed":true|false,"hostPath":H|null,
#            "containerPath":P|null,"folderDeleted":true|false,"recreate":[...]}
#   list    {"phase":"done","roots":[{"kind":"primary"|"extra"|"synced",
#            "id":ID|null,"hostPath":H,"containerPath":P,"bytes":N|null}]}
# or {"phase":"error","error":REASON,...}: bad_input (exit 2), owner_mismatch
# (exit 1, on Linux when the caller's uid isn't HERMES_UID), busy (exit 1),
# command_failed (exit 4). Everything else goes to stderr.
#
# bash 3.2 compatible.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
ENV_FILE="${ROOT}/.env"
OVERRIDE="${ROOT}/docker-compose.override.yml"
# shellcheck source=lib/json-lines.sh
source "${ROOT}/scripts/lib/json-lines.sh"
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"
# shellcheck source=lib/python.sh
source "${ROOT}/scripts/lib/python.sh"
# shellcheck source=lib/stack-lock.sh
source "${ROOT}/scripts/lib/stack-lock.sh"
# shellcheck source=lib/coding-roots.sh
source "${ROOT}/scripts/lib/coding-roots.sh"
jl_init

die() { jl_fail bad_input "" "$*"; }

# shellcheck source=lib/roots.sh
source "${ROOT}/scripts/lib/roots.sh"

# Ownership is real only on Linux; Docker Desktop maps it. Tests set this.
HOST_OS="${CODING_ROOT_UNAME:-$(uname -s)}"

path_covered() {
  local candidate="$1"
  local primary extras
  primary="$(env_get OPENCODE_WORKSPACE_HOST)"
  extras="$(env_get CODING_EXTRA_ROOTS)"
  [[ -n "$primary" ]] || return 1
  if [[ "$candidate" == "$primary" || "$candidate" == "$primary"/* ]]; then
    return 0
  fi
  local IFS=':'
  local root
  # shellcheck disable=SC2086
  for root in $extras; do
    [[ -z "$root" ]] && continue
    if [[ "$candidate" == "$root" || "$candidate" == "$root"/* ]]; then
      return 0
    fi
  done
  return 1
}

# recreate_services: what to recreate after a coding root change. The binds sit
# on the terminal service and opencode; CODING_EXTRA_ROOTS feeds opencode-mcp's
# root check and hermes' HERMES_WRITE_SAFE_ROOT, which write_file and patch
# check even when the shell runs on hermes-worker.
recreate_services() {
  local service
  service="$(terminal_service)"
  if [[ "$service" == hermes ]]; then
    printf '%s\n' "hermes opencode opencode-mcp"
  else
    printf '%s\n' "${service} hermes opencode opencode-mcp"
  fi
}

# recreate_json: recreate_services as a JSON array.
recreate_json() {
  local s out=""
  for s in $(recreate_services); do
    out="${out:+${out},}\"${s}\""
  done
  printf '[%s]' "$out"
}

# lock: takes the config lock until exit, or ends with busy.
lock() {
  local lockfile rc=0 who
  lockfile="$(resolve_data_root_path)/addons/.lock"
  stack_lock "$lockfile" || rc=$?
  case "$rc" in
    0) trap stack_unlock EXIT ;;
    2) jl_fail busy "" "${lockfile} is not a lock symlink; remove it if no change is running" ;;
    *)
      who="$(stack_lock_busy_text)"
      jl_fail busy "" "another config change is running${who:+ (${who})}" "" ",\"holder\":$(stack_lock_holder_json)" ;;
  esac
}

require_env() {
  [[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE — run ./scripts/setup.sh first"
}

# extras_with PATH: CODING_EXTRA_ROOTS with PATH appended, unless it's there.
extras_with() {
  local extras
  extras="$(env_get CODING_EXTRA_ROOTS)"
  case ":${extras#:}:" in
    *:"$1":*) printf '%s\n' "$extras"; return ;;
  esac
  if [[ -z "$extras" || "$extras" == ":" ]]; then
    printf ':%s\n' "$1"
  else
    extras="${extras}:$1"
    [[ "$extras" == :* ]] || extras=":$extras"
    printf '%s\n' "$extras"
  fi
}

# drop_extra PATH: takes PATH out of CODING_EXTRA_ROOTS. Returns 1 when it
# wasn't there.
drop_extra() {
  local extras kept="" root found=0
  extras="$(env_get CODING_EXTRA_ROOTS)"
  local IFS=':'
  # shellcheck disable=SC2086
  for root in $extras; do
    [[ -z "$root" ]] && continue
    if [[ "$root" == "$1" ]]; then
      found=1
    else
      kept="${kept}:${root}"
    fi
  done
  unset IFS
  [[ "$found" == 1 ]] || return 1
  upsert_env "$ENV_FILE" CODING_EXTRA_ROOTS "$kept"
}

# synced_id_at PATH: the ID of the synced root bound at PATH, if any.
synced_id_at() {
  local reg id container
  reg="$(coding_roots_registry)"
  [[ -s "$reg" ]] || return 0
  while IFS=$'\t' read -r id container _; do
    [[ "$container" == "$1" ]] && printf '%s\n' "$id"
  done < <(coding_roots_py --mount "$(coding_roots_dir)" list "$reg")
  return 0
}

# bytes_of DIR: DIR's disk use in bytes as JSON, or null when it's missing.
bytes_of() {
  local kb
  if [[ -d "$1" ]] && kb="$(du -sk "$1" 2> /dev/null | cut -f1)" && [[ "$kb" =~ ^[0-9]+$ ]]; then
    printf '%s' "$((kb * 1024))"
  else
    printf 'null'
  fi
}

cmd_list() {
  local json="$JL_JSON" size=1 arg
  for arg in "$@"; do
    case "$arg" in
      --no-size) size=0 ;;
      *) die "unknown option for list: $arg" ;;
    esac
  done
  local primary extras reg rows=""
  primary="$(env_get OPENCODE_WORKSPACE_HOST)"
  extras="$(env_get CODING_EXTRA_ROOTS)"
  reg="$(coding_roots_registry)"
  if [[ -s "$reg" ]]; then
    rows="$(coding_roots_py --mount "$(coding_roots_dir)" list "$reg")" \
      || jl_fail command_failed "" "cannot read $reg"
  fi

  if [[ "$json" == 1 ]]; then
    local out="" root id container host bytes
    entry() {
      bytes=null
      [[ "$size" == 1 ]] && bytes="$(bytes_of "$3")"
      out="${out:+${out},}{\"kind\":\"$1\",\"id\":$(jl_str_or_null "$2"),\"hostPath\":$(jl_str "$3"),\"containerPath\":$(jl_str "$4"),\"bytes\":${bytes}}"
    }
    [[ -n "$primary" ]] && entry primary "" "$primary" "$primary"
    local IFS=':'
    # shellcheck disable=SC2086
    for root in $extras; do
      [[ -n "$root" ]] || continue
      IFS=$'\t' read -r id host <<< "$(printf '%s\n' "$rows" | awk -F'\t' -v p="$root" '$2 == p { print $1 "\t" $3; exit }')"
      if [[ -n "$id" ]]; then
        entry synced "$id" "$host" "$root"
      else
        entry extra "" "$root" "$root"
      fi
    done
    unset IFS
    # Registry rows whose container path isn't in CODING_EXTRA_ROOTS (an add
    # stopped halfway) are still synced roots; list them so remove can find them.
    while IFS=$'\t' read -r id container host _; do
      [[ -n "$id" ]] || continue
      case ":${extras#:}:" in *:"$container":*) continue ;; esac
      entry synced "$id" "$host" "$container"
    done <<< "$rows"
    jl_done "\"roots\":[${out}]"
    return 0
  fi

  echo "Primary coding root (OPENCODE_WORKSPACE_HOST):"
  echo "  ${primary:-(unset)}"
  echo "Extra coding roots (CODING_EXTRA_ROOTS):"
  if [[ -z "$extras" || "$extras" == ":" ]]; then
    echo "  (none)"
  else
    local IFS=':'
    local root
    # shellcheck disable=SC2086
    for root in $extras; do
      [[ -z "$root" ]] && continue
      echo "  $root"
    done
    unset IFS
  fi
  if [[ -n "$rows" ]]; then
    echo "Synced coding roots ($(coding_roots_dir)):"
    local id container host
    while IFS=$'\t' read -r id container host _; do
      [[ -n "$id" ]] || continue
      if [[ "$size" == 1 ]]; then
        echo "  ${id}: ${container} <- ${host} ($(du -sh "$host" 2> /dev/null | cut -f1 || echo missing))"
      else
        echo "  ${id}: ${container} <- ${host}"
      fi
    done <<< "$rows"
  fi
  if [[ -f "$OVERRIDE" ]]; then
    echo "Override file: $OVERRIDE"
  else
    echo "Override file: (missing — will be created on add)"
  fi
}

cmd_add() {
  local path="${1:-}"
  validate_path "$path"
  require_env

  local primary service
  primary="$(env_get OPENCODE_WORKSPACE_HOST)"
  [[ -n "$primary" ]] || die "OPENCODE_WORKSPACE_HOST is unset"
  service="$(terminal_service)"
  lock

  if path_covered "$path"; then
    echo "Already covered by an existing coding root: $path"
    cmd_list --no-size
    return 0
  fi

  local extras
  extras="$(extras_with "$path")"
  upsert_env "$ENV_FILE" CODING_EXTRA_ROOTS "$extras"
  ensure_override
  add_mount "$service" "$path"
  add_mount opencode "$path"

  echo
  echo "Added coding root: $path"
  echo "CODING_EXTRA_ROOTS=$extras"
  echo
  echo "Recreate containers so the new binds and HERMES_WRITE_SAFE_ROOT apply:"
  echo "  docker compose up -d --force-recreate $(recreate_services)"
  echo "  # or: make down && make up"
  echo
  echo "If this tree has .env / credential files, shadow them (see docs/opencode.md):"
  echo "  find \"$path\" -name '.env' -not -path '*/node_modules/*'"
}

# make_folder DIR: creates a synced root's folder, owned so the coding agent
# (HERMES_UID) and the caller (the SSH user a client syncs as) can both write
# it. On Linux they must be the same user: root ignores group bits and
# OpenCode's umask isn't ours, so a shared group would not hold.
make_folder() {
  local dir="$1" uid gid
  uid="$(env_get HERMES_UID 1000)"
  gid="$(env_get HERMES_GID 1000)"
  if [[ "$HOST_OS" == Linux && "$(id -u)" != "$uid" ]]; then
    jl_fail owner_mismatch "" "you run as uid $(id -u), but the coding agent runs as HERMES_UID=${uid}; run this as that user so you can both write ${dir}"
  fi
  mkdir -p "$dir" || jl_fail command_failed "" "cannot create $dir"
  if [[ "$HOST_OS" == Linux && "$(id -g)" != "$gid" ]]; then
    chgrp "$gid" "$dir" || jl_fail command_failed "" "cannot give $dir to group HERMES_GID=${gid}"
  fi
  chmod 2775 "$dir" || jl_fail command_failed "" "cannot set the mode of $dir"
}

cmd_add_synced() {
  local path="" id=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --container-path) [[ $# -ge 2 ]] || die "--container-path needs a path"; path="$2"; shift 2 ;;
      --id) [[ $# -ge 2 ]] || die "--id needs an ID"; id="$2"; shift 2 ;;
      *) die "unknown option for add: $1" ;;
    esac
  done
  [[ -n "$path" && -n "$id" ]] || die "add needs both --container-path PATH and --id ID"
  require_env
  local primary service dir reg
  primary="$(env_get OPENCODE_WORKSPACE_HOST)"
  [[ -n "$primary" ]] || die "OPENCODE_WORKSPACE_HOST is unset"
  service="$(terminal_service)"
  dir="$(coding_roots_dir)"
  reg="${dir}/roots.json"
  lock

  local verdict status host created=true
  verdict="$(coding_roots_py --mount "$dir" check "$reg" "$id" "$path" "$primary" \
    "$(env_get CODING_EXTRA_ROOTS)" "$(env_get CONTEXT_EXTRA_ROOTS)" "$ROOT" \
    "$(resolve_data_root_path)")" || jl_fail command_failed "" "cannot read $reg"
  status="${verdict%%$'\t'*}"
  case "$status" in
    new) host="${dir}/${id}" ;;
    exists) host="${verdict#*$'\t'}"; created=false ;;
    error) die "${verdict#*$'\t'}" ;;
    *) jl_fail command_failed "" "unexpected answer from coding_roots.py: $verdict" ;;
  esac

  # Each step repeats safely, so a second add finishes one that stopped halfway.
  make_folder "$host"
  if [[ "$created" == true ]]; then
    coding_roots_py --mount "$dir" put "$reg" "$id" "$path" "$host" \
      || jl_fail command_failed "" "cannot write $reg"
  fi
  upsert_env "$ENV_FILE" CODING_EXTRA_ROOTS "$(extras_with "$path")"
  ensure_override
  add_mount "$service" "${host}:${path}"
  add_mount opencode "${host}:${path}"

  jl_done "\"id\":$(jl_str "$id"),\"hostPath\":$(jl_str "$host"),\"containerPath\":$(jl_str "$path"),\"created\":${created},\"recreate\":$(recreate_json)"
  if [[ "$JL_JSON" != 1 ]]; then
    echo
    if [[ "$created" == true ]]; then
      echo "Added synced coding root ${id}: ${host} at ${path}"
    else
      echo "Synced coding root ${id} already exists: ${host} at ${path}"
    fi
    echo
    echo "Recreate containers so the new binds and HERMES_WRITE_SAFE_ROOT apply:"
    echo "  docker compose up -d --force-recreate $(recreate_services)"
  fi
}

# cmd_remove DIR: the reverse of add for an extra root. The folder itself and
# any shadow mounts under it in the override are left alone.
cmd_remove() {
  local path="${1:-}"
  [[ -n "$path" ]] || die "path is required"
  [[ "$path" == /* ]] || die "path must be absolute: $path"
  [[ "$path" != */ ]] || die "path must not end with a slash: $path"
  require_env
  local service id
  service="$(terminal_service)"
  if [[ "$path" == "$(env_get OPENCODE_WORKSPACE_HOST)" ]]; then
    die "$path is the primary coding root; change OPENCODE_WORKSPACE_HOST in .env instead"
  fi
  lock
  id="$(synced_id_at "$path")"
  [[ -z "$id" ]] || die "$path is synced coding root ${id}; use remove --id ${id}"

  if ! drop_extra "$path"; then
    echo "Not an extra coding root: $path"
    cmd_list --no-size
    return 0
  fi
  remove_mount "$service" "$path"
  remove_mount opencode "$path"

  echo
  echo "Removed coding root: $path"
  echo "CODING_EXTRA_ROOTS=$(env_get CODING_EXTRA_ROOTS)"
  echo
  echo "Recreate containers so the binds and HERMES_WRITE_SAFE_ROOT drop it:"
  echo "  docker compose up -d --force-recreate $(recreate_services)"
}

cmd_remove_synced() {
  local id="" delete=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --id) [[ $# -ge 2 ]] || die "--id needs an ID"; id="$2"; shift 2 ;;
      --delete-folder) delete=1; shift ;;
      *) die "unknown option for remove: $1" ;;
    esac
  done
  [[ -n "$id" ]] || die "remove needs --id ID or a directory"
  require_env
  local service dir reg row container host deleted=false
  service="$(terminal_service)"
  dir="$(coding_roots_dir)"
  reg="${dir}/roots.json"
  lock

  row=""
  if [[ -s "$reg" ]]; then
    row="$(coding_roots_py --mount "$dir" get "$reg" "$id")" || jl_fail command_failed "" "cannot read $reg"
  fi
  if [[ -z "$row" ]]; then
    jl_done "\"id\":$(jl_str "$id"),\"removed\":false,\"hostPath\":null,\"containerPath\":null,\"folderDeleted\":false,\"recreate\":[]"
    [[ "$JL_JSON" == 1 ]] || echo "No synced coding root with ID ${id}; nothing to remove"
    return 0
  fi
  container="${row%%$'\t'*}"
  host="${row#*$'\t'}"

  drop_extra "$container" || true
  remove_mount "$service" "${host}:${container}"
  remove_mount opencode "${host}:${container}"
  coding_roots_py --mount "$dir" drop "$reg" "$id" > /dev/null || jl_fail command_failed "" "cannot write $reg"
  if [[ "$delete" == 1 && -d "$host" ]]; then
    # Only ever a folder this script made: <data root>/coding-roots/<ID>.
    [[ "$(basename "$host")" == "$id" && "$(basename "$(dirname "$host")")" == coding-roots ]] \
      || jl_fail command_failed "" "not deleting $host: it isn't a coding-roots/${id} folder"
    rm -rf "$host" || jl_fail command_failed "" "cannot delete $host"
    deleted=true
  fi

  jl_done "\"id\":$(jl_str "$id"),\"removed\":true,\"hostPath\":$(jl_str "$host"),\"containerPath\":$(jl_str "$container"),\"folderDeleted\":${deleted},\"recreate\":$(recreate_json)"
  if [[ "$JL_JSON" != 1 ]]; then
    echo
    echo "Removed synced coding root ${id}: ${container}"
    if [[ "$deleted" == true ]]; then
      echo "Deleted ${host}"
    else
      echo "The folder stays: ${host}"
    fi
    echo
    echo "Recreate containers so the binds and HERMES_WRITE_SAFE_ROOT drop it:"
    echo "  docker compose up -d --force-recreate $(recreate_services)"
  fi
}

usage() {
  cat <<EOF
Usage: $0 <list|add|remove> ...

  list [--json] [--no-size]
      Show the primary, extra and synced coding roots
  add DIR
      Grant Hermes + OpenCode access to an absolute directory
  add --container-path PATH --id ID [--json]
      Create a synced root's folder under <data root>/coding-roots/ID and
      bind it at PATH, which needn't exist on this host
  remove DIR
      Take an extra root away again (the folder stays)
  remove --id ID [--delete-folder] [--json]
      Take a synced root away; the folder stays unless --delete-folder

EOF
}

main() {
  local cmd="${1:-}" args=() a
  [[ $# -gt 0 ]] && shift
  for a in "$@"; do
    case "$a" in
      --json) JL_JSON=1 ;;
      *) args+=("$a") ;;
    esac
  done
  # JSON goes to fd 3 (jl_init); the mount editors' chatter goes to stderr.
  [[ "$JL_JSON" == 1 ]] && exec 1>&2
  case "$cmd" in
    list) cmd_list ${args[@]+"${args[@]}"} ;;
    add)
      if [[ "${args[0]:-}" == --* ]]; then
        cmd_add_synced "${args[@]}"
      else
        cmd_add "${args[0]:-}"
      fi
      ;;
    remove)
      if [[ "${args[0]:-}" == --* ]]; then
        cmd_remove_synced "${args[@]}"
      else
        cmd_remove "${args[0]:-}"
      fi
      ;;
    -h|--help|help) usage ;;
    "") usage; exit 1 ;;
    *) die "unknown command: $cmd" ;;
  esac
}

main "$@"
