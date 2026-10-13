#!/usr/bin/env bash
# Set up the messages-triage Hermes cron job. See docs/messages.md.
#
#   scripts/messages-triage-cron.sh create [--schedule "every 30m"] [--deliver signal] [--prompt-file FILE]
#   scripts/messages-triage-cron.sh show
#   scripts/messages-triage-cron.sh remove
#
# The job runs on the default profile with the messages tools only: no web,
# browser, terminal or other MCP servers. New-message events fire it at once
# through the default profile's `messages` webhook route; the schedule is a
# safety net, and Hermes skips the model on those ticks unless the messages
# MCP's watermark shows unreported messages. Its report goes only to --deliver
# (default: signal when SIGNAL_HOME_CHANNEL is set, else local). `create`
# replaces an existing messages-triage job.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

die() { printf 'messages-triage-cron: %s\n' "$1" >&2; exit 1; }
usage() { sed -n '4,6p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
env_get() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }

[[ $# -ge 1 ]] || usage
action="$1"; shift
case "$action" in create|show|remove) ;; *) usage ;; esac

schedule="every 30m"
deliver=""
prompt_file="compose/hermes/messages-triage-prompt.md"
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
  case "$(env_get MESSAGES_MCP_ENABLED)" in
    1|true|TRUE|yes|YES) ;;
    *) die "MESSAGES_MCP_ENABLED isn't 1; run ./scripts/setup.sh --messages first" ;;
  esac
  grep -qs 'messages-mcp:8000' data/hermes/config.yaml \
    || die "the default Hermes profile has no messages MCP; run make hermes-reload"
  grep -qs 'messages_triage_batch' data/hermes/config.yaml \
    || die "the default profile doesn't list messages_triage_batch; run make hermes-reload"
  [[ -f "$prompt_file" ]] || die "prompt file not found: $prompt_file"
  if [[ -z "$deliver" ]]; then
    if [[ -n "$(env_get SIGNAL_HOME_CHANNEL)" ]]; then deliver="signal"; else deliver="local"; fi
  fi
  [[ -n "$(env_get MESSAGES_WEBHOOK_SECRET)" ]] \
    || printf 'messages-triage-cron: no MESSAGES_WEBHOOK_SECRET, so new messages only show up on the schedule (%s)\n' "$schedule" >&2
fi
docker compose ps --status running hermes 2>/dev/null | grep -q hermes || die "hermes isn't running; make up"

request="$(python3 -c '
import json, sys
action, schedule, deliver, prompt_file = sys.argv[1:5]
req = {"action": action, "name": "messages-triage", "toolsets": ["messages"]}
if action == "create":
    req.update(schedule=schedule, deliver=deliver, prompt=open(prompt_file).read(),
               monitor_url="http://messages-mcp:8000/watermark")
print(json.dumps(req))
' "$action" "$schedule" "$deliver" "$prompt_file")"

docker compose exec -T -u hermes -w /tmp hermes /opt/hermes/.venv/bin/python - "$request" \
  < scripts/lib/hermes_mail_triage.py
if [[ "$action" == "create" && "$deliver" == "local" ]]; then
  printf 'messages-triage-cron: reports stay in Hermes (deliver=local); set SIGNAL_HOME_CHANNEL or pass --deliver to get them sent to you\n' >&2
fi
