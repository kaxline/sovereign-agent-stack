#!/usr/bin/env bash
# Rerun the bootstrap's cron fence (fence_mail_from_cron) on the running
# install, after a client adds or removes MCP servers in a profile's
# config.yaml. Hermes rereads config.yaml on each cron run, so nothing restarts.
# docs/cron-fence.md has the rules.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

usage() {
  cat <<'EOF'
Usage: ./scripts/cron-fence.sh [--profile NAME]

Pin platform_toolsets.cron again from each profile's current mcp_servers, so a
cron job without its own enabled_toolsets gets the servers added since the last
bootstrap run and still doesn't get the fenced ones. Run it after your
config.yaml writes have finished, not while a bootstrap is running.

  --profile NAME   Only this profile ("default" for the default profile).
                   Without it, every profile.
  -h, --help       Show this help

Exit status: 0 done, 1 the bootstrap container failed, 2 bad arguments,
3 no such profile.
EOF
}

profile=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
      profile="$2"
      shift 2
      ;;
    --profile=*)
      profile="${1#--profile=}"
      [[ -n "$profile" ]] || { usage >&2; exit 2; }
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

if [[ -n "$profile" && ! "$profile" =~ ^[A-Za-z0-9_][A-Za-z0-9_-]*$ ]]; then
  printf '[cron-fence] ERROR: not a profile name: %s\n' "$profile" >&2
  exit 2
fi

status=0
docker compose run --rm --no-deps \
  -e HERMES_BOOTSTRAP_ONLY=cron-fence \
  -e HERMES_CRON_FENCE_PROFILE="$profile" \
  hermes-api-bootstrap < /dev/null || status=$?
case "$status" in
  0|2|3) exit "$status" ;;
  *) exit 1 ;;
esac
