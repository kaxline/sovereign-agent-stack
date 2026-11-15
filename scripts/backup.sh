#!/usr/bin/env bash
# Back up the stack's data, or restore a backup.
#
#   scripts/backup.sh [create] [--dest DIR] [--stop] [--plan] [--json]
#   scripts/backup.sh restore --from DIR [--stop] [--with-dirs] [--json]
#   common: [--compose-file FILE]... [--project NAME]
#
# create writes DIR/<UTC timestamp>-<version>/ (DIR defaults to BACKUP_DIR,
# else ./backups), holding one .tar.gz per part, parts.tsv and manifest.json:
#
#   stack       the stack directory without data/, backups/, .git and
#               .claude/worktrees: code, .env and every local config file
#   data        ./data, without what can be rebuilt or is a backup already:
#               *.bak-* folders, backups/, worktree-backups/, and the host
#               mail and Messages indexes (mail/, messages/, messages-events/)
#   data-root   ASSISTANT_DATA_ROOT, when it is not ./data
#   dir-KEY     a *DATA_DIR in .env that points outside both of the above
#   volume-NAME each stateful named volume that exists: n8n_storage,
#               neo4j_data, opencode_config, opencode_data, gptr_outputs,
#               email_drafts. Caches (hermes_uv_cache, the Actual MCP caches,
#               search_gate_data, valkey_data, email_index, ollama_data) are
#               left out.
#
# Postgres (the db add-on, Vikunja, Twenty) and Hermes' SQLite files live in
# data/, so a copy is consistent only with the stack stopped. Both commands
# refuse while a service runs (error stack_running) unless --stop is given;
# create then starts the services it stopped again, and restore leaves them
# stopped.
#
# restore unpacks each part into where it belongs now: data into ./data,
# data-root into the current ASSISTANT_DATA_ROOT, whatever path the backup
# recorded. dir-KEY parts are outside the stack (signal-cli's store, say) and
# are restored only with --with-dirs, and only while .env still names the same
# path. It refuses a backup stored inside a directory it would replace. Each
# directory is moved aside to PATH.pre-restore-<timestamp> before unpacking,
# then what create left out (mail indexes, older backups) is moved back from
# there; it deletes nothing. A volume's current contents are
# saved to <backup>/pre-restore-<timestamp>/ before it is emptied and unpacked.
# The stack part is not restored; unpack stack.tar.gz by hand if needed.
#
# tar runs as root in the pinned Hermes image, so it can read files the
# database containers own on Linux, and keeps owners and modes; the archives
# are then handed to the calling user. BACKUP_TAR=host runs the host's tar
# instead (tests). Backups hold every secret the stack has: keep them as
# private as .env.
#
# --json prints JSON lines in addon.sh's shapes (docs/extending.md "JSON
# lines"): {"phase":"setup","step":PART,"status":"done",...} per part, then
# {"phase":"done",...} or {"phase":"error","error":REASON,"step":PART|null,
# "message":TEXT,"logTail":[...]}. REASON is bad_input (exit 2),
# stack_running (exit 1) or command_failed (exit 4). --plan prints the parts
# create would write, as {"phase":"part",...} lines, and writes nothing.
#
# bash 3.2 compatible.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"
# shellcheck source=lib/python.sh
source "${ROOT}/scripts/lib/python.sh"
# shellcheck source=lib/json-lines.sh
source "${ROOT}/scripts/lib/json-lines.sh"
jl_init

STATEFUL_VOLUMES="n8n_storage neo4j_data opencode_config opencode_data gptr_outputs email_drafts"
DATA_EXCLUDES="./backups ./worktree-backups ./*.bak-* ./mail ./messages ./messages-events"

CMD=create
WITH_DIRS=0
STOP=0
PLAN=0
DEST=""
FROM=""
files=()
if [[ $# -gt 0 && ( "$1" == create || "$1" == restore ) ]]; then
  CMD="$1"
  shift
fi

emit() { jl_emit "$1"; }
say() { [[ "$JL_JSON" == 1 ]] || printf '%s\n' "$1" >&2; }

# fail CODE MESSAGE: the error line for the part being worked on (STEP), with
# its log (FAIL_LOG) as logTail.
STEP=""
FAIL_LOG=""
fail() { jl_fail "$1" "$STEP" "$2" "$FAIL_LOG"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JL_JSON=1; shift ;;
    --stop) STOP=1; shift ;;
    --plan) PLAN=1; shift ;;
    --with-dirs) WITH_DIRS=1; shift ;;
    --dest) DEST="${2:-}"; shift 2 || fail bad_input "--dest needs a directory" ;;
    --from) FROM="${2:-}"; shift 2 || fail bad_input "--from needs a directory" ;;
    --compose-file) files+=("${2:-}"); shift 2 || fail bad_input "--compose-file needs a file" ;;
    --project) export COMPOSE_PROJECT_NAME="${2:-}"; shift 2 || fail bad_input "--project needs a name" ;;
    -h | --help) sed -n '2,49p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail bad_input "unknown option $1" ;;
  esac
done
if [[ ${#files[@]} -gt 0 ]]; then
  COMPOSE_FILE="$(IFS=:; printf '%s' "${files[*]}")"
  export COMPOSE_FILE
fi

abs_path() {
  local p="$1"
  [[ "$p" == "~" ]] && p="$HOME"
  # shellcheck disable=SC2088  # a literal ~/ prefix from .env
  [[ "${p:0:2}" == "~/" ]] && p="${HOME}/${p:2}"
  [[ "$p" == /* ]] || p="${ROOT}/${p#./}"
  printf '%s\n' "${p%/}"
}

# inside PATH DIR: whether PATH is DIR or under it.
inside() { [[ "$1" == "$2" || "$1" == "$2/"* ]]; }

stack_version() {
  local v=""
  if [[ -f .stack-version ]]; then
    v="$(head -1 .stack-version | tr -d '[:space:]')"
  elif [[ -e .git ]] && command -v git > /dev/null 2>&1; then
    v="$(git describe --tags --always 2> /dev/null || true)"
  fi
  printf '%s\n' "${v:-unknown}"
}

project_name() {
  if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
    printf '%s\n' "$COMPOSE_PROJECT_NAME"
    return
  fi
  docker compose config --format json 2> /dev/null \
    | sed -n 's/^  "name": "\(.*\)",$/\1/p' | head -1
}

# volume_for NAME: the project's volume for compose volume NAME, if it exists.
volume_for() {
  docker volume ls -q --filter "label=com.docker.compose.project=${PROJECT}" \
    --filter "label=com.docker.compose.volume=$1" 2> /dev/null | head -1 || true
}

running_services() {
  docker compose ps --services --status running 2> /dev/null || true
}

# --- the tar runner ----------------------------------------------------------

# tar_create SRC_KIND SRC OUT_DIR FILE [--exclude=PATTERN]...: SRC_KIND is dir
# (a host path) or volume (a volume name).
tar_create() {
  local kind="$1" src="$2" out="$3" file="$4"
  shift 4
  if [[ "${BACKUP_TAR:-}" == host ]]; then
    [[ "$kind" == volume ]] && src="$(docker volume inspect -f '{{.Mountpoint}}' "$src")"
    tar -czf "${out}/${file}" "$@" -C "$src" .
    return
  fi
  local mount="$src:/src:ro"
  docker run --rm --user 0 -v "$mount" -v "${out}:/dst" --entrypoint sh \
    "$(stack_hermes_image)" -c \
    'f="$1"; o="$2"; shift 2; tar -czf "/dst/$f" --numeric-owner "$@" -C /src . && chown "$o" "/dst/$f"' \
    sh "$file" "$(id -u):$(id -g)" "$@"
}

# tar_extract DST_KIND DST ARCHIVE: unpacks ARCHIVE into an empty DST.
tar_extract() {
  local kind="$1" dst="$2" archive="$3"
  if [[ "${BACKUP_TAR:-}" == host ]]; then
    [[ "$kind" == volume ]] && dst="$(docker volume inspect -f '{{.Mountpoint}}' "$dst")"
    find "$dst" -mindepth 1 -delete
    tar -xzpf "$archive" -C "$dst"
    return
  fi
  docker run --rm --user 0 -v "${dst}:/dst" -v "$(dirname "$archive"):/src:ro" \
    --entrypoint sh "$(stack_hermes_image)" -c \
    'find /dst -mindepth 1 -delete && tar -xzpf "/src/$1" --numeric-owner -C /dst' \
    sh "$(basename "$archive")"
}

file_bytes() { wc -c < "$1" | tr -d '[:space:]'; }

# --- parts ---------------------------------------------------------------

# Parallel arrays: kind (dir|volume), name, source, excludes (space-separated).
P_KIND=()
P_NAME=()
P_SRC=()
P_EXCL=()

add_part() {
  P_KIND+=("$1")
  P_NAME+=("$2")
  P_SRC+=("$3")
  P_EXCL+=("${4:-}")
}

collect_parts() {
  local data root dest_rel="" line key val path
  data="${ROOT}/data"
  root="$(resolve_data_root_path)"
  root="${root%/}"

  local stack_excl="./data ./backups ./.git ./.claude/worktrees ./.install-tmp.* ./.install.lock"
  if inside "$DEST" "$ROOT" && [[ "$DEST" != "${ROOT}/backups" ]]; then
    dest_rel="./${DEST#"${ROOT}"/}"
    stack_excl="${stack_excl} ${dest_rel}"
  fi
  add_part dir stack "$ROOT" "$stack_excl"

  local data_excl="$DATA_EXCLUDES"
  inside "$DEST" "$data" && data_excl="${data_excl} ./${DEST#"${data}"/}"
  [[ -d "$data" ]] && add_part dir data "$data" "$data_excl"

  if [[ -d "$root" ]] && ! inside "$root" "$data"; then
    local root_excl="./backups"
    inside "$DEST" "$root" && root_excl="${root_excl} ./${DEST#"${root}"/}"
    add_part dir data-root "$root" "$root_excl"
  fi

  if [[ -f .env ]]; then
    while IFS= read -r line; do
      key="${line%%=*}"
      val="$(env_get "$key")"
      [[ -n "$val" ]] || continue
      path="$(abs_path "$val")"
      [[ -d "$path" ]] || continue
      inside "$path" "$data" && continue
      inside "$path" "$root" && continue
      inside "$path" "$ROOT" && continue
      add_part dir "dir-${key}" "$path" ""
    done < <(grep -E '^[A-Z][A-Z0-9_]*DATA_DIR=' .env || true)
  fi

  local v vol
  for v in $STATEFUL_VOLUMES; do
    vol="$(volume_for "$v")"
    [[ -n "$vol" ]] && add_part volume "volume-${v}" "$vol" ""
  done
  return 0
}

# --- create ----------------------------------------------------------------

STOPPED_SERVICES=""
restart_stopped() {
  [[ -n "$STOPPED_SERVICES" ]] || return 0
  local svcs=()
  while IFS= read -r s; do [[ -n "$s" ]] && svcs+=("$s"); done <<< "$STOPPED_SERVICES"
  STOPPED_SERVICES=""
  say "Starting ${#svcs[@]} service(s) again"
  docker compose start "${svcs[@]}" > /dev/null 2>&1 \
    || printf 'warning: docker compose start failed; run make up\n' >&2
}

require_stopped() {
  local running
  running="$(running_services)"
  [[ -n "$running" ]] || return 0
  if [[ "$STOP" != 1 ]]; then
    fail stack_running "the stack is running ($(printf '%s' "$running" | tr '\n' ' ' | sed 's/ $//')); stop it or pass --stop"
  fi
  say "Stopping the stack"
  docker compose stop > /dev/null 2>&1 || fail command_failed "docker compose stop failed"
  printf '%s\n' "$running"
}

cmd_create() {
  DEST="$(abs_path "${DEST:-${BACKUP_DIR:-./backups}}")"
  PROJECT="$(project_name)"
  [[ -n "$PROJECT" ]] || fail command_failed "could not work out the compose project name"
  collect_parts

  local i excl
  if [[ "$PLAN" == 1 ]]; then
    for i in "${!P_KIND[@]}"; do
      emit "{\"phase\":\"part\",\"part\":$(jl_str "${P_NAME[$i]}"),\"kind\":\"${P_KIND[$i]}\",\"source\":$(jl_str "${P_SRC[$i]}"),\"exclude\":$(jl_str "${P_EXCL[$i]}")}"
      say "${P_NAME[$i]}: ${P_SRC[$i]}${P_EXCL[$i]:+ (without ${P_EXCL[$i]})}"
    done
    return 0
  fi

  STOPPED_SERVICES="$(require_stopped)" || exit $?
  trap restart_stopped EXIT

  local version stamp out total=0 bytes
  version="$(stack_version)"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  out="${DEST}/${stamp}-${version}"
  mkdir -p "$out" || fail command_failed "cannot create ${out}"
  chmod 700 "$out"
  : > "${out}/parts.tsv"

  for i in "${!P_KIND[@]}"; do
    local args=() ex=()
    # read -a splits without globbing: the patterns are for tar, not the shell.
    read -r -a ex <<< "${P_EXCL[$i]}"
    for excl in ${ex[@]+"${ex[@]}"}; do args+=("--exclude=${excl}"); done
    say "Backing up ${P_NAME[$i]}"
    STEP="${P_NAME[$i]}"
    FAIL_LOG="${out}/.log"
    jl_setup "$STEP" "running"
    if ! tar_create "${P_KIND[$i]}" "${P_SRC[$i]}" "$out" "${P_NAME[$i]}.tar.gz" \
      ${args[@]+"${args[@]}"} > "${out}/.log" 2>&1; then
      [[ "$JL_JSON" == 1 ]] || cat "${out}/.log" >&2
      fail command_failed "backing up ${P_NAME[$i]} (${P_SRC[$i]}) failed; the partial backup is in ${out}"
    fi
    bytes="$(file_bytes "${out}/${P_NAME[$i]}.tar.gz")"
    total=$((total + bytes))
    printf '%s\t%s\t%s\t%s.tar.gz\n' "${P_KIND[$i]}" "${P_NAME[$i]}" "${P_SRC[$i]}" "${P_NAME[$i]}" >> "${out}/parts.tsv"
    jl_setup "$STEP" "done" ",\"bytes\":${bytes}"
  done
  rm -f "${out}/.log"
  STEP=""
  FAIL_LOG=""

  {
    printf '{"version":%s,"created":"%s","project":%s,"parts":[' \
      "$(jl_str "$version")" "$stamp" "$(jl_str "$PROJECT")"
    local first=1 kind name src file
    while IFS=$'\t' read -r kind name src file; do
      [[ "$first" == 1 ]] || printf ','
      first=0
      printf '{"kind":"%s","name":"%s","source":%s,"file":"%s","bytes":%s}' \
        "$kind" "$name" "$(jl_str "$src")" "$file" "$(file_bytes "${out}/${file}")"
    done < "${out}/parts.tsv"
    printf ']}\n'
  } > "${out}/manifest.json"

  restart_stopped
  jl_done "\"path\":$(jl_str "$out"),\"version\":$(jl_str "$version"),\"parts\":${#P_KIND[@]},\"bytes\":${total}"
  say "Backup written to ${out}"
}

# --- restore ----------------------------------------------------------------

# restore_left_out ASIDE DST PATTERNS: moves the top-level entries create left
# out (PATTERNS, its excludes) from ASIDE back into DST.
restore_left_out() {
  local aside="$1" dst="$2" pat entry name pats=()
  read -r -a pats <<< "$3"
  for pat in "${pats[@]}"; do
    pat="${pat#./}"
    for entry in "${aside}"/$pat; do
      [[ -e "$entry" ]] || continue
      name="$(basename "$entry")"
      [[ -e "${dst}/${name}" ]] || mv "$entry" "${dst}/${name}"
    done
  done
}

cmd_restore() {
  [[ -n "$FROM" ]] || fail bad_input "restore needs --from DIR"
  FROM="$(abs_path "$FROM")"
  [[ -f "${FROM}/parts.tsv" ]] || fail bad_input "${FROM} has no parts.tsv; not a backup from scripts/backup.sh"
  PROJECT="$(project_name)"
  [[ -n "$PROJECT" ]] || fail command_failed "could not work out the compose project name"
  require_stopped > /dev/null || exit $?

  local stamp kind name src file aside vol n=0 key want excl plan
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  local data_now root_now
  data_now="${ROOT}/data"
  root_now="$(resolve_data_root_path)"
  root_now="${root_now%/}"

  # First pass: where each part goes now, and whether to restore it.
  plan="$(mktemp)"
  while IFS=$'\t' read -r kind name src file; do
    case "$name" in
      stack) continue ;;
      data) src="$data_now"; excl="$DATA_EXCLUDES" ;;
      data-root) src="$root_now"; excl="./backups" ;;
      dir-*)
        key="${name#dir-}"
        want="$(env_get "$key")"
        [[ -n "$want" ]] && want="$(abs_path "$want")"
        if [[ "$WITH_DIRS" != 1 ]]; then
          say "Skipping ${name} (${src}): outside the stack; pass --with-dirs to restore it"
          continue
        fi
        [[ "$want" == "$src" ]] \
          || fail bad_input "${name} was backed up from ${src}, but .env now says ${key}=${want:-unset}; not restoring it"
        excl="" ;;
      *) excl="" ;;
    esac
    [[ -f "${FROM}/${file}" ]] || fail bad_input "${FROM}/${file} is missing"
    if [[ "$kind" == dir ]] && inside "$FROM" "$src"; then
      fail bad_input "the backup is inside ${src}, which restore replaces; move it elsewhere first"
    fi
    printf '%s\t%s\t%s\t%s\t%s\n' "$kind" "$name" "$src" "$file" "$excl" >> "$plan"
  done < "${FROM}/parts.tsv"

  while IFS=$'\t' read -r kind name src file excl; do
    say "Restoring ${name}"
    STEP="$name"
    FAIL_LOG="${FROM}/.log"
    jl_setup "$STEP" "running"
    if [[ "$kind" == dir ]]; then
      aside=""
      if [[ -e "$src" ]]; then
        aside="${src}.pre-restore-${stamp}"
        local k=1
        while [[ -e "$aside" ]]; do aside="${src}.pre-restore-${stamp}-${k}"; k=$((k + 1)); done
        mv "$src" "$aside" || fail command_failed "cannot move ${src} aside"
      fi
      mkdir -p "$src"
      tar_extract dir "$src" "${FROM}/${file}" > /dev/null 2>"${FROM}/.log" || {
        [[ "$JL_JSON" == 1 ]] || cat "${FROM}/.log" >&2
        fail command_failed "unpacking ${name} into ${src} failed; the previous copy is ${aside:-absent}"
      }
      [[ -n "$aside" && -n "$excl" ]] && restore_left_out "$aside" "$src" "$excl"
      jl_setup "$STEP" "done" ",\"aside\":$(jl_str_or_null "$aside")"
    else
      vol="${name#volume-}"
      vol="$(volume_for "$vol")"
      if [[ -z "$vol" ]]; then
        vol="${PROJECT}_${name#volume-}"
        docker volume create --label "com.docker.compose.project=${PROJECT}" \
          --label "com.docker.compose.volume=${name#volume-}" "$vol" > /dev/null \
          || fail command_failed "cannot create volume ${vol}"
      else
        mkdir -p "${FROM}/pre-restore-${stamp}"
        tar_create volume "$vol" "${FROM}/pre-restore-${stamp}" "${name}.tar.gz" > /dev/null 2>&1 \
          || fail command_failed "saving the current ${vol} failed; nothing changed in it"
      fi
      tar_extract volume "$vol" "${FROM}/${file}" > /dev/null 2>"${FROM}/.log" || {
        [[ "$JL_JSON" == 1 ]] || cat "${FROM}/.log" >&2
        fail command_failed "unpacking ${name} into volume ${vol} failed"
      }
      jl_setup "$STEP" "done"
    fi
    n=$((n + 1))
  done < "$plan"
  rm -f "$plan" "${FROM}/.log"
  STEP=""
  FAIL_LOG=""
  jl_done "\"from\":$(jl_str "$FROM"),\"parts\":${n}"
  say "Restored ${n} part(s) from ${FROM}; the stack is stopped. Start it with make up."
}

case "$CMD" in
  create) cmd_create ;;
  restore) cmd_restore ;;
esac
