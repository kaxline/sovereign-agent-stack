#!/usr/bin/env bash
# scripts/lib/doctor-kit.sh: the counters behind ok/bad/warn, has_profile's
# reading of COMPOSE_PROFILES, and svc_running against a fake docker.
#
#   bash scripts/test-doctor-kit.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"

# shellcheck source=lib/doctor-kit.sh
source "${ROOT}/scripts/lib/doctor-kit.sh"

[ "$(ok fine)" = '  OK  fine' ] || fail "ok line"
[ "$(bad broke)" = ' FAIL broke' ] || fail "bad line"
[ "$(warn hmm)" = ' WARN hmm' ] || fail "warn line"
ok a > /dev/null; ok b > /dev/null; bad c > /dev/null; warn d > /dev/null
[ "$PASS/$FAIL/$WARN" = 2/1/1 ] || fail "counters: $PASS/$FAIL/$WARN"

# No .env: core only.
has_profile core || fail "core is the default profile"
if has_profile todo; then fail "todo without .env"; fi
printf 'COMPOSE_PROFILES=core, todo ,mail-drafts\n' > .env
has_profile todo || fail "todo with spaces around it"
has_profile mail-drafts || fail "mail-drafts"
if has_profile mail; then fail "mail matched mail-drafts"; fi

# compose lists running services by container name; the fake runs one.
mkdir bin
cat > bin/docker <<'SH'
#!/bin/sh
[ "$1 $2 $3 $4" = "compose ps --status running" ] || exit 1
[ "$5" = vikunja ] && printf 'NAME IMAGE\nassistant-vikunja-1 vikunja\n'
exit 0
SH
chmod +x bin/docker
PATH="${tmp}/bin:$PATH"
svc_running vikunja || fail "vikunja is running"
if svc_running mail-mcp; then fail "mail-mcp is not running"; fi

# check_flag_profile and check_flag_set: COMPOSE_PROFILES has todo and
# mail-drafts (above).
printf 'COMPOSE_PROFILES=core,todo\nVIKUNJA_MCP_ENABLED=1\nMAIL_MCP_ENABLED=true\n' > .env
[ "$(check_flag_profile VIKUNJA_MCP_ENABLED todo)" = '  OK  COMPOSE_PROFILES includes todo' ] || fail "profile on"
[ "$(check_flag_profile VIKUNJA_MCP_ENABLED todo 'custom')" = '  OK  custom' ] || fail "OKTEXT"
[ "$(check_flag_profile MAIL_MCP_ENABLED mail)" = " FAIL MAIL_MCP_ENABLED=1 but 'mail' not in COMPOSE_PROFILES — run make ensure-local" ] \
  || fail "flag on, profile off"
[ -z "$(check_flag_profile CALDAV_MCP_ENABLED calendar)" ] || fail "both off prints nothing"
[ "$(check_flag_set MAIL_MCP_ENABLED mail 'n')" = '  OK  MAIL_MCP_ENABLED=1' ] || fail "flag set"
[ "$(check_flag_set CALDAV_MCP_ENABLED calendar 'no tools')" = ' WARN calendar profile on but CALDAV_MCP_ENABLED is not 1 — no tools' ] \
  || fail "flag not set"

# check_profile_registers and check_api_server_clean.
mkdir -p data/hermes/profiles/api-server data/hermes/profiles/browser
printf 'mcp_servers:\n  mail:\n    url: http://mail-mcp:8000/mcp\n' > data/hermes/config.yaml
printf 'mcp_servers: {}\n' > data/hermes/profiles/browser/config.yaml
[ "$(check_profile_registers default 'mail-mcp:8000' 'the mail MCP' 'mail MCP')" = '  OK  Hermes default profile registers the mail MCP' ] \
  || fail "registers"
[ "$(check_profile_registers browser 'mail-mcp:8000' 'the mail MCP' 'mail MCP')" = ' WARN Hermes browser profile has no mail MCP entry — run make hermes-reload' ] \
  || fail "no entry"
[ -z "$(check_profile_registers browser 'mail-mcp:8000' 'the mail MCP')" ] || fail "no MISSING, no line"
[ -z "$(check_profile_registers api-server 'mail-mcp:8000' 'the mail MCP' 'mail MCP')" ] || fail "no config.yaml, no line"
[ -z "$(check_api_server_clean 'mail-mcp:8000' 'leak')" ] || fail "api-server has no config.yaml"
cp data/hermes/config.yaml data/hermes/profiles/api-server/config.yaml
[ "$(check_api_server_clean 'mail-mcp:8000' 'leak')" = ' FAIL leak' ] || fail "api-server registers mail"

# Every flag and profile pair passed to check_flag_profile or check_flag_set
# is a row of sync-profiles.sh --table.
table="$(bash "${ROOT}/scripts/sync-profiles.sh" --table | cut -d' ' -f1,2)"
calls="$(cd "$ROOT" && grep -hoE 'check_flag_(profile|set) [A-Z_]+ [a-z-]+' scripts/doctor.sh scripts/lib/doctor-*.sh | cut -d' ' -f2,3 | sort -u)"
[ -n "$calls" ] || fail "no check_flag_profile calls found"
while IFS= read -r pair; do
  printf '%s\n' "$table" | grep -qxF "$pair" || fail "${pair} is not a row of sync-profiles.sh --table"
done <<< "$calls"

echo "ok doctor-kit"
