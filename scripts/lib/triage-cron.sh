# shellcheck shell=bash
# The body of mail-triage-cron.sh and messages-triage-cron.sh. Source it after
# lib/env.sh, from the repo root, then call:
#
#   triage_cron NAME TOOLSET MCP_HOST DEFAULT_SCHEDULE PROMPT_FILE "$@"
#
# NAME is the job name (mail-triage), TOOLSET its only toolset (mail), and
# MCP_HOST the MCP service whose /watermark the job's monitor polls
# (mail-mcp). The caller may define these hooks, which run on `create` only
# and see $schedule, $deliver and $prompt_file:
#   triage_cron_gate   before the prompt-file check: the .env flag and the
#                      profile's config
#   triage_cron_notes  after --deliver gets its default: warnings that don't stop
#   triage_cron_ready  after Hermes is found running: checks run inside it
# The caller's usage text is lines 4-6 of its own header.

die() { printf '%s-cron: %s\n' "$_tc_name" "$1" >&2; exit 1; }
usage() { sed -n '4,6p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

triage_cron() {
  _tc_name="$1"
  local toolset="$2" host="$3"
  schedule="$4"
  prompt_file="$5"
  deliver=""
  shift 5

  [[ $# -ge 1 ]] || usage
  action="$1"; shift
  case "$action" in create|show|remove) ;; *) usage ;; esac
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --schedule) schedule="${2:-}"; shift 2 ;;
      --deliver) deliver="${2:-}"; shift 2 ;;
      --prompt-file) prompt_file="${2:-}"; shift 2 ;;
      *) usage ;;
    esac
  done

  [[ -f .env ]] || die "no .env; run ./scripts/setup.sh first"
  if [[ "$action" == "create" ]]; then
    if declare -F triage_cron_gate >/dev/null; then triage_cron_gate; fi
    [[ -f "$prompt_file" ]] || die "prompt file not found: $prompt_file"
    if [[ -z "$deliver" ]]; then
      if [[ -n "$(env_get SIGNAL_HOME_CHANNEL)" ]]; then deliver="signal"; else deliver="local"; fi
    fi
    if declare -F triage_cron_notes >/dev/null; then triage_cron_notes; fi
  fi
  docker compose ps --status running hermes 2>/dev/null | grep -q hermes || die "hermes isn't running; make up"
  if [[ "$action" == "create" ]] && declare -F triage_cron_ready >/dev/null; then triage_cron_ready; fi

  local request
  request="$(python3 -c '
import json, sys
action, name, toolset, schedule, deliver, prompt_file, monitor_url = sys.argv[1:8]
req = {"action": action, "name": name, "toolsets": [toolset]}
if action == "create":
    req.update(schedule=schedule, deliver=deliver, prompt=open(prompt_file).read(),
               monitor_url=monitor_url)
print(json.dumps(req))
' "$action" "$_tc_name" "$toolset" "$schedule" "$deliver" "$prompt_file" "http://${host}:8000/watermark")"

  docker compose exec -T -u hermes -w /tmp hermes /opt/hermes/.venv/bin/python - "$request" \
    < scripts/lib/hermes_triage_cron.py
  if [[ "$action" == "create" && "$deliver" == "local" ]]; then
    printf '%s-cron: reports stay in Hermes (deliver=local); set SIGNAL_HOME_CHANNEL or pass --deliver to get them sent to you\n' "$_tc_name" >&2
  fi
}
