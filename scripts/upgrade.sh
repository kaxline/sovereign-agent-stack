#!/usr/bin/env bash
# Run the upgrade hooks between two versions, then start the stack and apply
# what the hooks asked for.
#
#   scripts/upgrade.sh --from VERSION [--to VERSION] [--no-start] [--json]
#
# Run it after the new version's files are in place and scripts/setup.sh has
# run: scripts/install.sh does both, and in a git checkout it follows
# `git checkout vX.Y.Z && scripts/setup.sh`.
#
# Hooks are upgrades/vX.Y.Z.sh. Each runs once per upgrade that crosses its
# version (FROM < version <= TO), in version order, from the stack root, with
# STACK_FROM and STACK_TO set. upgrades/lib.sh lists the directives a hook
# prints. After the hooks, scripts/up.sh starts the stack with every requested
# rebuild and, if any hook asked, a forced Hermes reload. --no-start skips that.
#
# TO defaults to this tree's version (.stack-version, else the last tag `git
# describe` names). A downgrade is refused.
#
# --json prints JSON lines in addon.sh's shapes (docs/extending.md "JSON
# lines"): {"phase":"setup","step":"hook","target":V,"status":...} per hook,
# the same for step "start", then {"phase":"done","from":F,"to":T,
# "hooks":[...],"needs_user":[{"id","message","version"}],"notices":[...]} or
# {"phase":"error","error":REASON,"step":S|null,"message":TEXT,"logTail":[...]}.
# REASON is bad_input (exit 2) or command_failed (exit 4).
#
# bash 3.2 compatible.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FROM=""
TO=""
JSON=0
START=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM="${2:-}"; shift 2 || true ;;
    --to) TO="${2:-}"; shift 2 || true ;;
    --no-start) START=0; shift ;;
    --json) JSON=1; shift ;;
    -h | --help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'error: unknown option %s\n' "$1" >&2; exit 2 ;;
  esac
done

# shellcheck source=lib/json-lines.sh
source "${ROOT}/scripts/lib/json-lines.sh"
jl_init
[[ "$JSON" == 1 ]] && JL_JSON=1
say() { [[ "$JL_JSON" == 1 ]] || printf '%s\n' "$1" >&2; }
# fail CODE MESSAGE [STEP] [LOGFILE]
fail() { jl_fail "$1" "${3:-}" "$2" "${4:-}"; }

# version_key vX.Y.Z: a sortable key, or nothing for another shape. A `git
# describe` suffix (-N-gSHA) is dropped: a commit after a tag runs that
# tag's hooks.
version_key() {
  local v="${1%%-*}"
  [[ "$v" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || return 1
  printf '%06d%06d%06d\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
}

this_version() {
  if [[ -f .stack-version ]]; then
    head -1 .stack-version | tr -d '[:space:]'
  elif [[ -e .git ]]; then
    git describe --tags --match 'v[0-9]*' 2> /dev/null || true
  fi
}

[[ -n "$FROM" ]] || fail bad_input "--from VERSION is required"
[[ -n "$TO" ]] || TO="$(this_version)"
[[ -n "$TO" ]] || fail bad_input "cannot tell this tree's version; pass --to"
from_key="$(version_key "$FROM")" || fail bad_input "--from ${FROM} is not vX.Y.Z"
to_key="$(version_key "$TO")" || fail bad_input "--to ${TO} is not vX.Y.Z"
[[ ! "$to_key" < "$from_key" ]] || fail bad_input "${TO} is older than ${FROM}; downgrades are not supported"

# The hooks to run, in version order.
hooks=()
while IFS=$'\t' read -r _ f; do
  [[ -n "$f" ]] && hooks+=("$f")
done < <(
  for f in upgrades/v*.sh; do
    [[ -f "$f" ]] || continue
    v="$(basename "$f" .sh)"
    k="$(version_key "$v")" || continue
    [[ "$k" > "$from_key" && ! "$k" > "$to_key" ]] && printf '%s\t%s\n' "$k" "$f"
  done | sort
)

builds=()
reload=0
ran=()
needs_json=""
notices_json=""
log="$(mktemp)"
trap 'rm -f "$log"' EXIT

for f in ${hooks[@]+"${hooks[@]}"}; do
  v="$(basename "$f" .sh)"
  say "Upgrade hook ${v}"
  jl_setup hook "running" ",\"target\":\"${v}\""
  if ! out="$(STACK_FROM="$FROM" STACK_TO="$TO" bash "$f" 2> "$log")"; then
    [[ "$JL_JSON" == 1 ]] || cat "$log" >&2
    fail command_failed "upgrade hook ${v} failed: $(tail -1 "$log")" hook "$log"
  fi
  [[ "$JL_JSON" == 1 || ! -s "$log" ]] || cat "$log" >&2
  ran+=("$v")
  while IFS=$'\t' read -r kind a b; do
    case "$kind" in
      rebuild) builds+=("$a") ;;
      reload_hermes) reload=1 ;;
      needs_user)
        needs_json="${needs_json:+${needs_json},}{\"id\":$(jl_str "$a"),\"message\":$(jl_str "$b"),\"version\":\"${v}\"}"
        say "ACTION NEEDED (${v}): ${b}" ;;
      notice)
        notices_json="${notices_json:+${notices_json},}{\"message\":$(jl_str "$a"),\"version\":\"${v}\"}"
        say "Note (${v}): ${a}" ;;
      "") ;;
      *) fail command_failed "upgrade hook ${v} printed an unknown directive: ${kind}" hook ;;
    esac
  done <<< "$out"
  jl_setup hook "done" ",\"target\":\"${v}\""
done

if [[ "$START" == 1 ]]; then
  args=()
  # Unique, in order.
  for s in ${builds[@]+"${builds[@]}"}; do
    case " ${args[*]-} " in *" --build ${s} "*) ;; *) args+=(--build "$s") ;; esac
  done
  [[ "$reload" == 1 ]] && args+=(--reload-hermes)
  say "Starting the stack"
  jl_setup start "running"
  if [[ "$JL_JSON" == 1 ]]; then
    bash ./scripts/up.sh ${args[@]+"${args[@]}"} > "$log" 2>&1 \
      || fail command_failed "starting the stack failed: $(tail -1 "$log")" start "$log"
  else
    bash ./scripts/up.sh ${args[@]+"${args[@]}"} >&2 || fail command_failed "starting the stack failed" start
  fi
  jl_setup start "done"
fi

hooks_json=""
for v in ${ran[@]+"${ran[@]}"}; do hooks_json="${hooks_json:+${hooks_json},}\"${v}\""; done
jl_done "\"from\":$(jl_str "$FROM"),\"to\":$(jl_str "$TO"),\"hooks\":[${hooks_json}],\"needs_user\":[${needs_json}],\"notices\":[${notices_json}]"
say "Upgraded ${FROM} -> ${TO} (${#ran[@]} hook(s))"
