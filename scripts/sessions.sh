#!/usr/bin/env bash
# Move chat history between installs: export every profile's Hermes sessions,
# then import them into another stack's matching profiles.
#
#   scripts/sessions.sh export --dest DIR [--profile NAME]... [--json]
#   scripts/sessions.sh import --from DIR [--profile NAME]... [--json]
#
# Both run against the stack's running hermes service, as the hermes user.
# export writes DIR/<profile>.jsonl (mode 0600) for the default profile and
# each profile under profiles/, or only the --profile ones, plus
# DIR/manifest.json naming each file's profile, session and message counts,
# the stack version and the Hermes image. DIR must be empty or new. The files
# hold whole transcripts, unredacted: keep them as private as a backup.
#
# import reads the manifest and loads each file into the profile of the same
# name through Hermes' SessionDB.import_sessions, while the gateway runs. A
# profile the target doesn't have is skipped and reported as profile_missing;
# its sessions never go into another profile. Session ids are kept. A session
# already present is skipped, so a second import of the same export imports
# nothing. A session deleted on the target is refused, one over Hermes'
# per-session limits (10,000 messages or 5 MB) is too_large, and a child whose
# parent is in neither the target nor the export is imported detached. A
# session whose title the target, or an earlier session in the import, already
# holds is imported under that title plus " (<last 12 characters of its id>)"
# and counted as renamed (titles are unique in a Hermes store).
#
# Each id read appears in exactly one of an import line's first six id lists
# (a repeated id also in invalid_ids); renamed_ids is a subset of imported_ids. The manifest's profile entries carry
# session_ids, so the sessions of a profile_missing profile can be named too.
#
# --json prints JSON lines (docs/extending.md "JSON lines") holding counts and
# session ids only, never titles or message text:
#
#   {"phase":"profile","profile":P,"sessions":N,"messages":M}          export
#   {"phase":"profile","profile":P,"sessions":N,"imported":N,"skipped":N,
#    "detached":N,"renamed":N,"refused":N,"too_large":N,"invalid":N,
#    "failed":N,"imported_ids":[...],"skipped_ids":[...],"refused_ids":[...],
#    "too_large_ids":[...],"invalid_ids":[...],"failed_ids":[...],
#    "renamed_ids":[...]}                                              import
#   {"phase":"profile","profile":P,"sessions":N,"status":"profile_missing"}
#   {"phase":"done","action":"export","dest":DIR,"sessions":N,"messages":M}
#   {"phase":"done","action":"import","imported":N,...,"profile_missing":[P]}
#
# or {"phase":"error",...} with bad_input (exit 2), hermes_not_running
# (exit 1) or command_failed (exit 4). An import whose done line has
# failed > 0 exits 1.
#
# bash 3.2 compatible.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
# shellcheck source=lib/python.sh
source "${ROOT}/scripts/lib/python.sh"
# shellcheck source=lib/json-lines.sh
source "${ROOT}/scripts/lib/json-lines.sh"
jl_init

MODULE="${ROOT}/scripts/lib/sessions_move.py"
CMD="${1:-}"
[[ $# -gt 0 ]] && shift
DEST=""
FROM=""
PROFILES=()
STEP=""
LOG=""

say() { [[ "$JL_JSON" == 1 ]] || printf '%s\n' "$1" >&2; }
fail() { jl_fail "$1" "$STEP" "$2" "$LOG"; }

case "$CMD" in
  export | import) ;;
  -h | --help) sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) fail bad_input "usage: scripts/sessions.sh export --dest DIR | import --from DIR" ;;
esac

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JL_JSON=1; shift ;;
    --dest) DEST="${2:-}"; shift 2 || fail bad_input "--dest needs a directory" ;;
    --from) FROM="${2:-}"; shift 2 || fail bad_input "--from needs a directory" ;;
    --profile) PROFILES+=("${2:-}"); shift 2 || fail bad_input "--profile needs a name" ;;
    -h | --help) sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail bad_input "unknown option $1" ;;
  esac
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
LOG="${TMP}/log"
: > "$LOG"

# hermes_py ARGS...: the module in the hermes container. SESSIONS_PY replaces
# the command that runs python there (tests point it at a throwaway container).
hermes_py() {
  # shellcheck disable=SC2086 # SESSIONS_PY is a command line
  ${SESSIONS_PY:-docker compose exec -T -u hermes -w /opt/hermes hermes /opt/hermes/.venv/bin/python} \
    -c "$(cat "$MODULE")" "$@"
}

stack_version() {
  local v=""
  if [[ -f .stack-version ]]; then
    v="$(head -1 .stack-version | tr -d '[:space:]')"
  elif [[ -e .git ]] && command -v git > /dev/null 2>&1; then
    v="$(git describe --tags --always 2> /dev/null || true)"
  fi
  printf '%s\n' "${v:-unknown}"
}

abs_path() {
  local p="$1"
  [[ "$p" == /* ]] || p="${ROOT}/${p#./}"
  printf '%s\n' "${p%/}"
}

# num KEY LINE: the integer value of KEY in the JSON object LINE.
num() { printf '%s' "$2" | sed -n "s/.*\"$1\":\([0-9]*\).*/\1/p"; }

selected() {
  [[ ${#PROFILES[@]} -eq 0 ]] && return 0
  local p
  for p in "${PROFILES[@]}"; do [[ "$p" == "$1" ]] && return 0; done
  return 1
}

target_profiles() {
  STEP=profiles
  hermes_py profiles < /dev/null 2>> "$LOG" || fail hermes_not_running "could not run python in the hermes service; start the stack with make up"
}

cmd_export() {
  [[ -n "$DEST" ]] || fail bad_input "export needs --dest DIR"
  DEST="$(abs_path "$DEST")"
  if [[ -e "$DEST" ]]; then
    [[ -d "$DEST" && -z "$(ls -A "$DEST")" ]] || fail bad_input "${DEST} is not an empty directory"
  fi
  local names
  names="$(target_profiles)" || exit $?
  mkdir -p "$DEST" || fail command_failed "cannot create ${DEST}"
  chmod 700 "$DEST"

  local name entries="" total_s=0 total_m=0 summary s m ids found=0
  while IFS= read -r name; do
    if [[ -z "$name" ]] || ! selected "$name"; then continue; fi
    found=$((found + 1))
    STEP="$name"
    : > "${TMP}/err"
    ( umask 077; hermes_py export "$name" < /dev/null > "${DEST}/${name}.jsonl" 2> "${TMP}/err" ) \
      || { cat "${TMP}/err" >> "$LOG"; fail command_failed "export of profile ${name} failed"; }
    summary="$(sed -n 's/^@@sessions_move //p' "${TMP}/err" | tail -1)"
    [[ -n "$summary" ]] || { cat "${TMP}/err" >> "$LOG"; fail command_failed "export of profile ${name} gave no summary"; }
    s="$(num sessions "$summary")"
    m="$(num messages "$summary")"
    # The JSON list after "session_ids", the summary's last key.
    ids="$(printf '%s' "$summary" | sed -n 's/.*,"session_ids":\(\[.*\]\)}$/\1/p')"
    [[ -n "$ids" ]] || { cat "${TMP}/err" >> "$LOG"; fail command_failed "export of profile ${name} gave no session ids"; }
    total_s=$((total_s + s))
    total_m=$((total_m + m))
    entries="${entries:+${entries},}{\"name\":$(jl_str "$name"),\"file\":$(jl_str "${name}.jsonl"),\"sessions\":${s},\"messages\":${m},\"session_ids\":${ids}}"
    jl_emit "{\"phase\":\"profile\",\"profile\":$(jl_str "$name"),\"sessions\":${s},\"messages\":${m}}"
    say "${name}: ${s} session(s), ${m} message(s)"
  done <<< "$names"
  STEP=""
  [[ "$found" -gt 0 ]] || fail bad_input "no such profile here: ${PROFILES[*]}"

  ( umask 077; printf '{"format":"assistant-sessions/1","created_at":"%s","stack_version":%s,"hermes_image":%s,"profiles":[%s]}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(jl_str "$(stack_version)")" "$(jl_str "$(stack_hermes_image)")" \
    "$entries" > "${DEST}/manifest.json" )
  jl_done "\"action\":\"export\",\"dest\":$(jl_str "$DEST"),\"sessions\":${total_s},\"messages\":${total_m}"
  say "Exported ${total_s} session(s) to ${DEST}"
}

cmd_import() {
  [[ -n "$FROM" ]] || fail bad_input "import needs --from DIR"
  FROM="$(abs_path "$FROM")"
  [[ -f "${FROM}/manifest.json" ]] || fail bad_input "${FROM} has no manifest.json"
  local plan names
  STEP=manifest
  plan="$(hermes_py manifest < "${FROM}/manifest.json" 2>> "$LOG")" \
    || fail bad_input "${FROM}/manifest.json is not a sessions export"
  names="$(target_profiles)" || exit $?

  local name file count line missing="" k
  local imported=0 skipped=0 detached=0 renamed=0 refused=0 too_large=0 invalid=0 failed=0
  while IFS=$'\t' read -r name file count; do
    if [[ -z "$name" ]] || ! selected "$name"; then continue; fi
    STEP="$name"
    [[ -f "${FROM}/${file}" ]] || fail bad_input "${FROM}/${file} is missing"
    if ! printf '%s\n' "$names" | grep -qxF "$name"; then
      missing="${missing:+${missing},}$(jl_str "$name")"
      jl_emit "{\"phase\":\"profile\",\"profile\":$(jl_str "$name"),\"sessions\":${count},\"status\":\"profile_missing\"}"
      say "${name}: no such profile here, ${count} session(s) not imported"
      continue
    fi
    line="$(hermes_py import "$name" < "${FROM}/${file}" 2>> "$LOG")" \
      || fail command_failed "import into profile ${name} failed"
    [[ "$line" == '{"phase":"profile"'* ]] \
      || fail command_failed "import into profile ${name} gave no result"
    jl_emit "$line"
    for k in imported skipped detached renamed refused too_large invalid failed; do
      eval "$k=\$(( $k + $(num "$k" "$line") ))"
    done
    say "${name}: $(num imported "$line") imported (renamed $(num renamed "$line")), $(num skipped "$line") skipped, $(num refused "$line") refused, $(num too_large "$line") too large, $(num failed "$line") failed"
  done <<< "$plan"
  STEP=""

  jl_done "\"action\":\"import\",\"imported\":${imported},\"skipped\":${skipped},\"detached\":${detached},\"renamed\":${renamed},\"refused\":${refused},\"too_large\":${too_large},\"invalid\":${invalid},\"failed\":${failed},\"profile_missing\":[${missing}]"
  say "Imported ${imported} session(s), skipped ${skipped}"
  [[ "$failed" -eq 0 ]] || exit 1
}

"cmd_${CMD}"
