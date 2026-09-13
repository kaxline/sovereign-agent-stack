#!/usr/bin/env bash
# Manage ASSISTANT_DATA_ROOT: show / set / migrate user content directories.
# Hermes agent state stays at ./data/hermes. See docs/data-dir.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
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

upsert_env() {
  local file="$1"
  local key="$2"
  local value="$3"
  touch "$file"
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    local tmp
    tmp="$(mktemp)"
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" == "${key}="* ]]; then
        printf '%s=%s\n' "$key" "$value"
      else
        printf '%s\n' "$line"
      fi
    done < "$file" > "$tmp"
    mv "$tmp" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

expand_user_path() {
  local raw="$1"
  [[ -n "$raw" ]] || die "path required"
  if [[ "$raw" == "~" ]]; then
    raw="$HOME"
  elif [[ "$raw" == "~/"* ]]; then
    raw="$HOME/${raw#~/}"
  fi
  if [[ "$raw" != /* ]]; then
    raw="${ROOT}/${raw#./}"
  fi
  # Drop trailing slash
  raw="${raw%/}"
  if [[ -d "$raw" ]]; then
    (cd "$raw" && pwd -P)
  else
    local parent base
    parent="$(dirname "$raw")"
    base="$(basename "$raw")"
    if [[ -d "$parent" ]]; then
      echo "$(cd "$parent" && pwd -P)/${base}"
    else
      mkdir -p "$parent"
      echo "$(cd "$parent" && pwd -P)/${base}"
    fi
  fi
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

seed_voice_readme() {
  local dest="$1/voice/README.md"
  [[ -f "$dest" ]] && return 0
  cat > "$dest" <<'EOF'
# Writing voices

Each subdirectory here is one writing voice. Hermes reads them at `/opt/voice`.

## Setup

1. Create a voice and drop in samples:

       mkdir -p voice/my-voice/samples
       cp ~/writing/*.md voice/my-voice/samples/

   (Paths are relative to ASSISTANT_DATA_ROOT. See docs/data-dir.md.)

2. Calibrate in Hermes: `Calibrate the my-voice writing voice.`

3. Write: `Write a 600-word post about pricing in the my-voice voice.`

Full guide: docs/writing-voice.md in the assistant repo.
EOF
  log "Wrote $dest"
}

seed_projects_readme() {
  local dest="$1/projects/README.md"
  [[ -f "$dest" ]] && return 0
  cat > "$dest" <<'EOF'
# Projects

Each subdirectory here is one project. Hermes reads them at `/opt/projects`.

Scaffold from the assistant repo root:

    make project-init PROJECT=my-project

Select the project as a WebUI workspace, or name `/opt/projects/my-project` in a prompt.

Full convention: docs/projects.md
EOF
  log "Wrote $dest"
}

seed_memory_readme() {
  local root="$1"
  local dest="$root/memory/README.md"
  [[ -f "$dest" ]] && return 0
  cat > "$dest" <<'EOF'
# Curated memory

Long-form notes at `/opt/memory`. Tiny facts stay in Hermes USER.md / MEMORY.md
under ./data/hermes/memories/.

Fill these files yourself or ask Hermes to remember and consolidate.

Full guide: docs/memory.md
EOF
  log "Wrote $dest"
}

seed_memory_index() {
  local dest="$1/memory/INDEX.md"
  [[ -f "$dest" ]] && return 0
  cat > "$dest" <<'EOF'
# Memory index

| File | What it holds |
|---|---|
| people.md | People, relationships, how you know them |
| decisions.md | Decisions and why they superseded earlier ones |
| preferences.md | Durable preferences that did not fit USER.md |
| notes/ | One topic file per extra note |

## Notes
EOF
  log "Wrote $dest"
}

seed_memory_stub() {
  local dest="$1"
  local title="$2"
  local body="$3"
  [[ -f "$dest" ]] && return 0
  printf '# %s\n\n%s\n' "$title" "$body" > "$dest"
  log "Wrote $dest"
}

ensure_user_data_layout() {
  local root="$1"
  mkdir -p \
    "$root/projects" \
    "$root/voice" \
    "$root/memory/notes" \
    "$root/inputs" \
    "$root/rag_storage" \
    "$root/corpora"
  seed_voice_readme "$root"
  seed_projects_readme "$root"
  seed_memory_readme "$root"
  seed_memory_index "$root"
  seed_memory_stub "$root/memory/people.md" "People" "One section per person. Empty until something is remembered."
  seed_memory_stub "$root/memory/decisions.md" "Decisions" "One section per decision. Empty until something is remembered."
  seed_memory_stub "$root/memory/preferences.md" "Preferences" "Durable preferences that will not fit in USER.md."
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
  new="$(expand_user_path "$raw")"
  validate_data_root_path "$new" || exit 1

  ensure_user_data_layout "$new"
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
  from="$(expand_user_path "$from_raw")"
  to="$(expand_user_path "$to_raw")"
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

  ensure_user_data_layout "$to"
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
        rm -rf "$from/$name"
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
