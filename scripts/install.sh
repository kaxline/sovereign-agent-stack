#!/usr/bin/env bash
# Install the stack at a release into a folder, or upgrade that install.
#
#   scripts/install.sh --version vX.Y.Z --dir DIR
#       [--tarball FILE [--sha256 HEX]] [--compose-file FILE]... [--project NAME]
#       [--json] [-- SETUP_ARGS...]
#
# Self-contained: it can run on its own, copied to a host that has no stack
# yet. It needs bash, tar, gzip, sha256sum or shasum, the Docker CLI with
# Compose v2 and a running engine, and curl when it downloads. Podman is not
# supported.
#
# The release comes from --tarball (FILE.sha256 beside it or --sha256 gives the
# checksum), else it is downloaded from the GitHub release with its .sha256
# asset (STACK_RELEASE_BASE overrides the URL prefix). The checksum is always
# checked. It proves the download is intact, not who made it.
#
# Fresh install (DIR missing or empty): unpack, scripts/setup.sh
# --non-interactive SETUP_ARGS, scripts/up.sh, scripts/doctor.sh.
#
# Upgrade (DIR/.stack-version older than VERSION): stop the stack, back it up
# with scripts/backup.sh into DIR/backups, remove the files the old release
# shipped that the new one doesn't (.stack-files), unpack the new release over
# DIR, run setup.sh --non-interactive and scripts/upgrade.sh --from OLD (hooks,
# then start), then doctor.sh. .env, data/ and local config files stay. If a
# step after the backup fails, the old release's files and config come back
# from the backup and the old stack is started again. Data is not rolled back:
# the error names the backup to restore with `scripts/backup.sh restore`.
#
# The same VERSION again re-runs setup, the start and doctor. An older VERSION
# is refused, as is a DIR that is a git checkout (upgrade those with git and
# scripts/upgrade.sh).
#
# Published ports bind 127.0.0.1, as docker-compose.yml has them.
# --compose-file and --project set COMPOSE_FILE and COMPOSE_PROJECT_NAME for
# every step, for an app that runs the stack under its own overlay.
#
# --json prints one JSON object per line: {"event":"step","step":S,
# "status":"running"|"done"} per step, then {"event":"done","version":V,
# "previous_version":P|null,"fresh":B,"backup":PATH|null,"needs_user":[...],
# "notices":[...],"doctor":"passed"} or {"event":"error","error":REASON,
# "message":TEXT,"step":S,"logTail":TEXT,"rolledBack":B,"backup":PATH|null}.
# REASON is bad_input, unsupported_host, checksum_mismatch, busy,
# command_failed or doctor_failed. doctor_failed leaves the new stack running.
set -euo pipefail

VERSION=""
DIR=""
TARBALL=""
SHA=""
JSON=0
files=()
setup_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="${2:-}"; shift 2 || shift ;;
    --dir) DIR="${2:-}"; shift 2 || shift ;;
    --tarball) TARBALL="${2:-}"; shift 2 || shift ;;
    --sha256) SHA="${2:-}"; shift 2 || shift ;;
    --compose-file) files+=("${2:-}"); shift 2 || shift ;;
    --project) export COMPOSE_PROJECT_NAME="${2:-}"; shift 2 || shift ;;
    --json) JSON=1; shift ;;
    --) shift; setup_args=("$@"); break ;;
    -h | --help) sed -n '2,46p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'error: unknown option %s\n' "$1" >&2; exit 2 ;;
  esac
done
if [[ ${#files[@]} -gt 0 ]]; then
  COMPOSE_FILE="$(IFS=:; printf '%s' "${files[*]}")"
  export COMPOSE_FILE
fi

exec 3>&1
STEP=""
LOG=""
BACKUP=""
ROLLED_BACK=false
WORK=""
LOCK=""

json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\t'/ }"
  s="${s//$'\r'/}"
  s="${s//$'\n'/\\n}"
  printf '%s' "$s"
}
jstr() { if [[ -n "$1" ]]; then printf '"%s"' "$(json_escape "$1")"; else printf 'null'; fi; }
emit() { [[ "$JSON" == 1 ]] && printf '%s\n' "$1" >&3; return 0; }
say() { [[ "$JSON" == 1 ]] || printf '%s\n' "$1" >&2; }

cleanup() {
  [[ -n "$WORK" ]] && rm -rf "$WORK"
  [[ -n "$LOCK" ]] && rmdir "$LOCK" 2> /dev/null
  return 0
}
trap cleanup EXIT

fail() {
  local reason="$1" msg="$2" tail=""
  [[ -n "$LOG" && -s "$LOG" ]] && tail="$(tail -20 "$LOG")"
  emit "{\"event\":\"error\",\"error\":\"${reason}\",\"message\":$(jstr "$msg"),\"step\":$(jstr "$STEP"),\"logTail\":$(jstr "$tail"),\"rolledBack\":${ROLLED_BACK},\"backup\":$(jstr "$BACKUP")}"
  printf 'error: %s\n' "$msg" >&2
  [[ "$JSON" == 1 || -z "$tail" ]] || printf '%s\n' "$tail" >&2
  exit "${3:-1}"
}

# step NAME CMD...: runs CMD in DIR with output to the step's log.
step() {
  STEP="$1"
  shift
  emit "{\"event\":\"step\",\"step\":\"${STEP}\",\"status\":\"running\"}"
  say "== ${STEP}"
  LOG="${WORK}/${STEP}.log"
  if [[ "$JSON" == 1 ]]; then
    (cd "$DIR" && "$@") > "$LOG" 2>&1
  else
    (cd "$DIR" && "$@") 2>&1 | tee "$LOG" >&2
    return "${PIPESTATUS[0]}"
  fi
}
step_done() { emit "{\"event\":\"step\",\"step\":\"${STEP}\",\"status\":\"done\"}"; }

version_key() {
  [[ "$1" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || return 1
  printf '%06d%06d%06d\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
}

sha256_of() {
  if command -v sha256sum > /dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

# --- arguments and host --------------------------------------------------------

[[ -n "$VERSION" ]] || fail bad_input "--version vX.Y.Z is required" 2
new_key="$(version_key "$VERSION")" || fail bad_input "--version ${VERSION} is not vX.Y.Z" 2
[[ -n "$DIR" ]] || fail bad_input "--dir DIR is required" 2
[[ "$DIR" == /* ]] || DIR="$(pwd)/${DIR}"
DIR="${DIR%/}"
[[ -e "${DIR}/.git" ]] && fail bad_input "${DIR} is a git checkout; upgrade it with git checkout ${VERSION}, scripts/setup.sh and scripts/upgrade.sh" 2

OS="$(uname -s)"
STEP=check
for a in ${setup_args[@]+"${setup_args[@]}"}; do
  case "$a" in
    --mail | --mail-drafts | --messages)
      [[ "$OS" == Darwin ]] || fail unsupported_host "${a#--} reads host-built Apple Mail or Messages indexes and needs macOS" 2 ;;
  esac
done
for c in tar gzip docker; do
  command -v "$c" > /dev/null 2>&1 || fail unsupported_host "${c} is not installed"
done
command -v sha256sum > /dev/null 2>&1 || command -v shasum > /dev/null 2>&1 \
  || fail unsupported_host "neither sha256sum nor shasum is installed"
if docker --version 2> /dev/null | grep -qi podman; then
  fail unsupported_host "docker is Podman, which the stack is not tested on; install Docker Engine"
fi
docker compose version > /dev/null 2>&1 || fail unsupported_host "docker compose (Compose v2) is not available"
docker info > /dev/null 2>&1 || fail unsupported_host "the Docker engine is not running, or this user cannot reach it"

mkdir -p "$DIR" || fail bad_input "cannot create ${DIR}"
LOCK="${DIR}/.install.lock"
if ! mkdir "$LOCK" 2> /dev/null; then
  LOCK=""
  fail busy "another install is running in ${DIR} (remove ${DIR}/.install.lock if none is)"
fi
WORK="$(mktemp -d "${DIR}/.install-tmp.XXXXXX")"

PREV=""
if [[ -f "${DIR}/.stack-version" ]]; then
  PREV="$(head -1 "${DIR}/.stack-version" | tr -d '[:space:]')"
  prev_key="$(version_key "$PREV")" || fail bad_input "${DIR}/.stack-version says ${PREV}, not vX.Y.Z"
  [[ ! "$new_key" < "$prev_key" ]] || fail bad_input "${DIR} has ${PREV}, newer than ${VERSION}; downgrades are not supported" 2
elif [[ -n "$(find "$DIR" -mindepth 1 -maxdepth 1 ! -name .install.lock ! -name '.install-tmp.*' | head -1)" ]]; then
  fail bad_input "${DIR} is not empty and has no .stack-version, so it is not an install this script made" 2
fi
step_done

# --- fetch and verify ----------------------------------------------------------

name="sovereign-agent-stack-${VERSION}"
if [[ -z "$TARBALL" ]]; then
  STEP=fetch
  emit '{"event":"step","step":"fetch","status":"running"}'
  command -v curl > /dev/null 2>&1 || fail unsupported_host "curl is needed to download the release; or pass --tarball"
  base="${STACK_RELEASE_BASE:-https://github.com/kaxline/sovereign-agent-stack/releases/download}"
  TARBALL="${WORK}/${name}.tar.gz"
  LOG="${WORK}/fetch.log"
  curl -fsSL -o "$TARBALL" "${base}/${VERSION}/${name}.tar.gz" 2> "$LOG" \
    || fail command_failed "downloading ${base}/${VERSION}/${name}.tar.gz failed"
  if [[ -z "$SHA" ]]; then
    curl -fsSL -o "${TARBALL}.sha256" "${base}/${VERSION}/${name}.tar.gz.sha256" 2>> "$LOG" \
      || fail command_failed "downloading the release checksum failed"
  fi
  step_done
fi
STEP=verify
[[ -f "$TARBALL" ]] || fail bad_input "${TARBALL} does not exist" 2
if [[ -z "$SHA" ]]; then
  [[ -f "${TARBALL}.sha256" ]] || fail bad_input "no checksum: pass --sha256 or put ${TARBALL}.sha256 beside it" 2
  SHA="$(cut -d' ' -f1 < "${TARBALL}.sha256")"
fi
got="$(sha256_of "$TARBALL")"
[[ "$got" == "$(printf '%s' "$SHA" | tr '[:upper:]' '[:lower:]')" ]] \
  || fail checksum_mismatch "${TARBALL} has sha256 ${got}, expected ${SHA}"
mkdir -p "${WORK}/unpacked"
tar -xzf "$TARBALL" -C "${WORK}/unpacked" || fail bad_input "${TARBALL} is not a readable tar.gz"
NEW="${WORK}/unpacked/${name}"
[[ -f "${NEW}/.stack-version" && -f "${NEW}/.stack-files" ]] \
  || fail bad_input "${TARBALL} is not a stack release (no ${name}/.stack-version or .stack-files)"
[[ "$(head -1 "${NEW}/.stack-version" | tr -d '[:space:]')" == "$VERSION" ]] \
  || fail bad_input "${TARBALL} is $(head -1 "${NEW}/.stack-version"), not ${VERSION}"
emit '{"event":"step","step":"verify","status":"done"}'

# --- install the files -----------------------------------------------------------

# copy_release SRC: unpacks SRC's files over DIR.
copy_release() { (cd "$1" && tar -cf - .) | (cd "$DIR" && tar -xf -); }

# remove_dropped OLD_LIST NEW_LIST: deletes DIR/path for each path in OLD_LIST
# and not in NEW_LIST. Paths that leave DIR are skipped.
remove_dropped() {
  local p
  LC_ALL=C comm -23 <(LC_ALL=C sort "$1") <(LC_ALL=C sort "$2") | while IFS= read -r p; do
    case "/${p}/" in */../* | //*) continue ;; esac
    rm -f "${DIR}/${p}"
  done
}

needs='[]'
notices='[]'
upgrade_result() {
  local line
  line="$(grep '"event":"done"' "$LOG" | tail -1 || true)"
  [[ -n "$line" ]] || return 0
  needs="$(printf '%s' "$line" | sed -n 's/.*"needs_user":\(\[.*\]\),"notices".*/\1/p')"
  notices="$(printf '%s' "$line" | sed -n 's/.*"notices":\(\[.*\]\)}$/\1/p')"
  [[ -n "$needs" ]] || needs='[]'
  [[ -n "$notices" ]] || notices='[]'
}

run_doctor() {
  if step doctor bash scripts/doctor.sh; then
    step_done
  else
    fail doctor_failed "scripts/doctor.sh reported problems; the stack is running"
  fi
}

finish() {
  emit "{\"event\":\"done\",\"version\":\"${VERSION}\",\"previous_version\":$(jstr "$PREV"),\"fresh\":$1,\"backup\":$(jstr "$BACKUP"),\"needs_user\":${needs},\"notices\":${notices},\"doctor\":\"passed\"}"
  say "Installed ${VERSION} in ${DIR}"
}

if [[ -z "$PREV" ]]; then
  STEP=files
  copy_release "$NEW" || fail command_failed "copying the release into ${DIR} failed"
  step setup bash scripts/setup.sh --non-interactive ${setup_args[@]+"${setup_args[@]}"} \
    || fail command_failed "scripts/setup.sh failed"
  step_done
  step start bash scripts/up.sh || fail command_failed "starting the stack failed"
  step_done
  run_doctor
  finish true
  exit 0
fi

# Upgrade (or the same version again).
step stop docker compose stop || fail command_failed "stopping the stack failed"
step_done
if [[ "$PREV" != "$VERSION" ]]; then
  step backup bash scripts/backup.sh create --dest "${DIR}/backups" --json \
    || fail command_failed "the backup failed; nothing was changed"
  BACKUP="$(sed -n 's/.*"event":"done","path":"\([^"]*\)".*/\1/p' "$LOG" | tail -1)"
  [[ -n "$BACKUP" && -f "${BACKUP}/stack.tar.gz" ]] || fail command_failed "the backup reported no stack archive; nothing was changed"
  step_done
fi

# rollback: puts the old release's files and config back and starts it.
rollback() {
  [[ -n "$BACKUP" ]] || return 0
  say "Rolling back to ${PREV}"
  remove_dropped "${NEW}/.stack-files" "${WORK}/old-files"
  if tar -xzf "${BACKUP}/stack.tar.gz" -C "$DIR" > /dev/null 2>&1; then
    ROLLED_BACK=true
    (cd "$DIR" && bash scripts/up.sh) > "${WORK}/rollback-start.log" 2>&1 \
      || printf 'warning: starting %s again failed; see %s\n' "$PREV" "${WORK}/rollback-start.log" >&2
  fi
}

upgrade_fail() {
  local reason="$1" msg="$2"
  rollback
  fail "$reason" "${msg}$([[ -n "$BACKUP" ]] && printf '; data changes are not rolled back, restore them with scripts/backup.sh restore --from %s --stop' "$BACKUP")"
}

STEP=files
cp "${DIR}/.stack-files" "${WORK}/old-files" 2> /dev/null || : > "${WORK}/old-files"
remove_dropped "${WORK}/old-files" "${NEW}/.stack-files"
copy_release "$NEW" || upgrade_fail command_failed "copying the release into ${DIR} failed"
emit '{"event":"step","step":"files","status":"done"}'

step setup bash scripts/setup.sh --non-interactive ${setup_args[@]+"${setup_args[@]}"} \
  || upgrade_fail command_failed "scripts/setup.sh failed"
step_done
step upgrade bash scripts/upgrade.sh --from "$PREV" --to "$VERSION" --json \
  || upgrade_fail command_failed "scripts/upgrade.sh failed"
upgrade_result
step_done
run_doctor
finish false
