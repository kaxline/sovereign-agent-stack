#!/usr/bin/env bash
# Set up the mail-triage Hermes cron job. See docs/email.md.
#
#   scripts/mail-triage-cron.sh create [--schedule "every 15m"] [--deliver signal] [--prompt-file FILE]
#   scripts/mail-triage-cron.sh show
#   scripts/mail-triage-cron.sh remove
#
# The job runs on the default profile with the mail tools only: no web,
# browser, terminal, drafts or other MCP servers. Hermes checks the mail
# index's watermark first and skips the model unless newer mail was indexed.
# Its report goes only to --deliver (default: signal when SIGNAL_HOME_CHANNEL
# is set, else local). `create` replaces an existing mail-triage job.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

die() { printf 'mail-triage-cron: %s\n' "$1" >&2; exit 1; }
usage() { sed -n '4,6p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
env_get() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }

[[ $# -ge 1 ]] || usage
action="$1"; shift
case "$action" in create|show|remove) ;; *) usage ;; esac

schedule="every 15m"
deliver=""
prompt_file="compose/hermes/mail-triage-prompt.md"
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
  case "$(env_get MAIL_MCP_ENABLED)" in
    1|true|TRUE|yes|YES) ;;
    *) die "MAIL_MCP_ENABLED isn't 1; run ./scripts/setup.sh --mail first" ;;
  esac
  grep -qs 'mail-mcp:8000' data/hermes/config.yaml \
    || die "the default Hermes profile has no mail MCP; re-run hermes-api-bootstrap and hermes-browser-bootstrap"
  [[ -f "$prompt_file" ]] || die "prompt file not found: $prompt_file"
  if [[ -z "$deliver" ]]; then
    if [[ -n "$(env_get SIGNAL_HOME_CHANNEL)" ]]; then deliver="signal"; else deliver="local"; fi
  fi
fi
docker compose ps --status running hermes 2>/dev/null | grep -q hermes || die "hermes isn't running; make up"

request="$(python3 -c '
import json, sys
action, schedule, deliver, prompt_file = sys.argv[1:5]
req = {"action": action}
if action == "create":
    req.update(schedule=schedule, deliver=deliver, prompt=open(prompt_file).read(),
               monitor_url="http://mail-mcp:8000/watermark")
print(json.dumps(req))
' "$action" "$schedule" "$deliver" "$prompt_file")"

docker compose exec -T -u hermes -w /tmp hermes /opt/hermes/.venv/bin/python - "$request" \
  < scripts/lib/hermes_mail_triage.py
if [[ "$action" == "create" && "$deliver" == "local" ]]; then
  printf 'mail-triage-cron: reports stay in Hermes (deliver=local); set SIGNAL_HOME_CHANNEL or pass --deliver to get them sent to you\n' >&2
fi
