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

echo "ok doctor-kit"
