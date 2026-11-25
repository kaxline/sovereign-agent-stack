#!/usr/bin/env bash
# Manage ASSISTANT_DATA_ROOT: show / set / migrate user content directories.
# Hermes agent state stays at ./data/hermes. See docs/data-dir.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"

REMOVE_SOURCE=0
FORCE=0

usage() {
  cat <<'EOF'
Usage: ./scripts/data-dir.sh <command> [args]

Commands:
  show                         Print current ASSISTANT_DATA_ROOT and subdir status
  set <path>                   Point ASSISTANT_DATA_ROOT at <path> and create layout
                               (does not move existing files)
  migrate <to>                 Copy user dirs from current root → <to>, then retarget
  migrate <from> <to>          Copy user dirs from <from> → <to>, then retarget

Options (migrate):
  --remove-source              After a successful copy, delete the six user dirs
                               from the source root (never touches hermes/)
  --force                      Allow migrate while stack containers are running

Examples:
  make data-dir-show
  make data-dir-set DIR=~/AssistantData
  make down && make data-dir-migrate DIR=~/AssistantData
  ./scripts/data-dir.sh migrate ./data ~/AssistantData --remove-source

User content under the root: projects voice memory inputs rag_storage corpora
Hermes state stays at: ./data/hermes
OpenCode / Signal use their own env vars (unchanged).
EOF
}

log() { printf '[data-dir] %s\n' "$1"; }
die() { printf '[data-dir] ERROR: %s\n' "$1" >&2; exit 1; }

# resolve_dir_arg PATH: PATH from the command line as an absolute, resolved
# path. Creates its parent so the result is stable; never PATH itself.
resolve_dir_arg() {
  local path
  [[ -n "$1" ]] || die "path required"
  path="$(expand_user_path "$1")"
  mkdir -p "$(dirname "$path")"
  resolve_data_root_path "$path"
}

current_data_root() {
  resolve_data_root_path ""
}

copy_tree() {
  local src="$1"
  local dst="$2"
  mkdir -p "$dst"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "$src/" "$dst/"
  else
    cp -a "$src/." "$dst/"
  fi
}

root_has_user_content() {
  local root="$1"
  local name
  for name in "${USER_DATA_SUBDIRS[@]}"; do
    if [[ -d "$root/$name" ]] && [[ -n "$(find "$root/$name" -mindepth 1 -maxdepth 1 2>/dev/null | head -1)" ]]; then
      return 0
    fi
  done
  return 1
}

cmd_show() {
  local root hermes name status
  root="$(current_data_root)"
  hermes="${ROOT}/data/hermes"
  echo "ASSISTANT_DATA_ROOT (resolved): $root"
  echo "Configured value:              $(data_root_env_get ASSISTANT_DATA_ROOT ./data)"
  echo
  echo "User content subdirs:"
  for name in "${USER_DATA_SUBDIRS[@]}"; do
    if [[ -d "$root/$name" ]]; then
      status="ok"
    else
      status="missing"
    fi
    printf '  %-12s %s  %s/%s\n' "$status" "$name" "$root" "$name"
  done
  echo
  echo "Hermes agent state (not relocated):"
  if [[ -d "$hermes" ]]; then
    echo "  ok           hermes  $hermes"
  else
    echo "  missing      hermes  $hermes"
  fi
  echo
  echo "OpenCode: OPENCODE_WORKSPACE_HOST=$(data_root_env_get OPENCODE_WORKSPACE_HOST '(unset)')"
  echo "Signal:   SIGNAL_CLI_DATA_DIR=$(data_root_env_get SIGNAL_CLI_DATA_DIR '(unset)')"
}

cmd_set() {
  local raw="${1:-}"
  [[ -n "$raw" ]] || die "Usage: data-dir.sh set <path>"
  local prev new
  prev="$(current_data_root)"
  new="$(resolve_dir_arg "$raw")"
  validate_data_root_path "$new" || exit 1

  seed_data_root "$new"
  upsert_env "${ROOT}/.env" ASSISTANT_DATA_ROOT "$new"
  log "ASSISTANT_DATA_ROOT=$new"

  if [[ "$prev" != "$new" ]] && root_has_user_content "$prev"; then
    log "Previous root still has content: $prev"
    log "This command did not move files. To copy them over:"
    log "  make down && ./scripts/data-dir.sh migrate \"$prev\" \"$new\""
  fi
  log "Recreate containers so binds refresh: make down && make up"
}

running_user_data_services() {
  local out
  out="$(docker compose ps --status running --services 2>/dev/null || true)"
  [[ -z "$out" ]] && return 1
  local svc
  for svc in hermes hermes-webui lightrag n8n gpt-researcher; do
    if printf '%s\n' "$out" | grep -qx "$svc"; then
      echo "$svc"
    fi
  done
}

cmd_migrate() {
  local from_raw="" to_raw=""
  if [[ $# -eq 1 ]]; then
    from_raw="$(data_root_env_get ASSISTANT_DATA_ROOT ./data)"
    to_raw="$1"
  elif [[ $# -eq 2 ]]; then
    from_raw="$1"
    to_raw="$2"
  else
    die "Usage: data-dir.sh migrate <to> | migrate <from> <to>"
  fi

  local from to
  from="$(resolve_dir_arg "$from_raw")"
  to="$(resolve_dir_arg "$to_raw")"
  validate_data_root_path "$to" || exit 1

  if path_is_inside "$to" "$from" || path_is_inside "$from" "$to"; then
    die "Source and destination must not nest: from=$from to=$to"
  fi
  if [[ "$from" == "$to" ]]; then
    die "Source and destination are the same: $from"
  fi

  local running
  running="$(running_user_data_services || true)"
  if [[ -n "$running" && "$FORCE" -ne 1 ]]; then
    die "Stop the stack before migrating (running: $(echo "$running" | tr '\n' ' ')). Or pass --force. Prefer: make down"
  fi
  if [[ -n "$running" && "$FORCE" -eq 1 ]]; then
    log "WARNING: migrating while containers are running: $(echo "$running" | tr '\n' ' ')"
  fi

  [[ -d "$from" ]] || die "Source root does not exist: $from"
  mkdir -p "$to"

  local name src dst
  for name in "${USER_DATA_SUBDIRS[@]}"; do
    src="$from/$name"
    dst="$to/$name"
    if [[ ! -d "$src" ]]; then
      log "skip (missing): $src"
      mkdir -p "$dst"
      continue
    fi
    log "copy $src → $dst"
    copy_tree "$src" "$dst"
  done

  seed_data_root "$to"
  upsert_env "${ROOT}/.env" ASSISTANT_DATA_ROOT "$to"
  log "ASSISTANT_DATA_ROOT=$to"

  echo
  log "Verification:"
  for name in "${USER_DATA_SUBDIRS[@]}"; do
    if [[ -d "$to/$name" ]]; then
      local size
      size="$(du -sh "$to/$name" 2>/dev/null | awk '{print $1}')"
      printf '  %-12s %s\t%s\n' "$name" "$size" "$to/$name"
    fi
  done

  if [[ "$REMOVE_SOURCE" -eq 1 ]]; then
    log "Removing source user dirs under $from (hermes untouched)"
    for name in "${USER_DATA_SUBDIRS[@]}"; do
      if [[ -d "$from/$name" ]]; then
        rm -rf "${from:?}/${name:?}"
        log "removed $from/$name"
      fi
    done
  else
    log "Source copies left in place under $from"
    log "After smoke-test, remove with: ./scripts/data-dir.sh migrate \"$from\" \"$to\" --remove-source"
    log "Or: rm -rf $from/projects $from/voice $from/memory $from/inputs $from/rag_storage $from/corpora"
  fi

  log "Recreate containers: make down && make up"
  log "Hermes state remains at ${ROOT}/data/hermes"
}

# --- arg parse ---
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --remove-source) REMOVE_SOURCE=1; shift ;;
    --force) FORCE=1; shift ;;
    -*) die "Unknown option: $1" ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

if [[ ${#ARGS[@]} -eq 0 ]]; then
  usage
  exit 1
fi

cmd="${ARGS[0]}"
case "$cmd" in
  show)
    cmd_show
    ;;
  set)
    [[ ${#ARGS[@]} -ge 2 ]] || die "Usage: data-dir.sh set <path>"
    cmd_set "${ARGS[1]}"
    ;;
  migrate)
    if [[ ${#ARGS[@]} -eq 2 ]]; then
      cmd_migrate "${ARGS[1]}"
    elif [[ ${#ARGS[@]} -eq 3 ]]; then
      cmd_migrate "${ARGS[1]}" "${ARGS[2]}"
    else
      die "Usage: data-dir.sh migrate <to> | migrate <from> <to>"
    fi
    ;;
  *)
    usage
    die "Unknown command: $cmd"
    ;;
esac
