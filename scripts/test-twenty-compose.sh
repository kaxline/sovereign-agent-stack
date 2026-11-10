#!/bin/sh
# The grep patterns below match compose's literal ${...} text, so single quotes are intended.
# shellcheck disable=SC2016
# Shape of the crm profile (local Twenty) in docker-compose.yml, and the flag
# that turns it on. See docs/crm.md.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
compose="${ROOT}/docker-compose.yml"

# One service's block, from its name to the next top-level service.
block() {
  awk -v name="  $1:" '
    $0 == name { on = 1; print; next }
    on && /^  [a-z0-9_-]+:$/ { exit }
    on && /^[a-z]/ { exit }
    on { print }
  ' "$compose"
}

for svc in twenty-server twenty-worker twenty-db twenty-redis; do
  b="$(block "$svc")"
  [ -n "$b" ] || fail "${svc} is missing"
  printf '%s\n' "$b" | grep -q 'profiles: \["crm"\]' || fail "${svc} must be in the crm profile only"
done

block twenty-server | grep -qF -- '- "127.0.0.1:${TWENTY_PORT:-3000}:3000"' \
  || fail "twenty-server publishes 127.0.0.1:\${TWENTY_PORT:-3000}:3000"
for svc in twenty-worker twenty-db twenty-redis; do
  if block "$svc" | grep -q '^    ports:'; then fail "${svc} must not publish a port"; fi
done

block twenty-worker | grep -qF 'DISABLE_DB_MIGRATIONS=true' || fail "worker must not run migrations"
block twenty-worker | grep -qF 'command: ["yarn", "worker:prod"]' || fail "worker runs yarn worker:prod"

[ "$(grep -cF -- '- TELEMETRY_ENABLED=false' "$compose")" = 2 ] \
  || fail "server and worker both set TELEMETRY_ENABLED=false"
[ "$(grep -cF -- '- IS_MULTIWORKSPACE_ENABLED=${TWENTY_MULTIWORKSPACE_BOOL:-false}' "$compose")" = 2 ] \
  || fail "server and worker both take IS_MULTIWORKSPACE_ENABLED from TWENTY_MULTIWORKSPACE_BOOL"

for svc in twenty-db twenty-redis; do
  block "$svc" | grep -qF 'networks: [crm]' || fail "${svc} is on the crm network only"
done
sed -n '/^networks:/,/^volumes:/p' "$compose" | grep -q '^  crm:' || fail "crm network is declared"

[ "$(grep -cF -- '- TWENTY_MCP_ENABLED=${TWENTY_MCP_ENABLED:-0}' "$compose")" = 2 ] \
  || fail "both hermes-*-bootstrap services receive TWENTY_MCP_ENABLED"
[ "$(grep -cF -- '- TWENTY_LOCAL_ENABLED=${TWENTY_LOCAL_ENABLED:-0}' "$compose")" = 2 ] \
  || fail "both hermes-*-bootstrap services receive TWENTY_LOCAL_ENABLED"
[ "$(grep -cF -- '- ${ASSISTANT_DATA_ROOT:-./data}/addons/twenty:/bootstrap/addon-data/twenty:ro' "$compose")" = 2 ] \
  || fail "both hermes-*-bootstrap services mount the Twenty instance store read-only"
[ "$(grep -cF -- '- ADDON_DATA_DIR=/bootstrap/addon-data' "$compose")" = 2 ] \
  || fail "both hermes-*-bootstrap services receive ADDON_DATA_DIR"
if grep -qF -- '/bootstrap/twenty-workspaces' "$compose"; then fail "no service mounts the old workspace files"; fi

# The rows come from addons/twenty/addon.json. TWENTY_MCP_ENABLED has a profile
# no service uses, so a remote-only install does not start the local services.
table="$(bash "${ROOT}/scripts/sync-profiles.sh" --table)"
printf '%s\n' "$table" | grep -qx 'TWENTY_LOCAL_ENABLED crm 0' \
  || fail "sync-profiles.sh maps TWENTY_LOCAL_ENABLED to crm"
printf '%s\n' "$table" | grep -qx 'TWENTY_MCP_ENABLED crm-mcp 0' \
  || fail "sync-profiles.sh maps TWENTY_MCP_ENABLED to crm-mcp"

# setup.sh: --crm turns both flags on; the secrets are generated for every
# install so enabling crm later needs no extra step, and never replaced.
setup="${ROOT}/scripts/setup.sh"
grep -qE '^    --crm\) SETUP_CRM=1; shift ;;$' "$setup" || fail "setup.sh accepts --crm"
grep -qF 'set_env_if_placeholder .env TWENTY_ENCRYPTION_KEY "$(openssl rand -base64 32)"' "$setup" \
  || fail "setup.sh generates TWENTY_ENCRYPTION_KEY once"
grep -qF 'set_env_if_placeholder .env TWENTY_DB_PASSWORD "$(rand_hex 32)"' "$setup" \
  || fail "setup.sh generates a hex TWENTY_DB_PASSWORD once"
for k in TWENTY_LOCAL_ENABLED TWENTY_MCP_ENABLED TWENTY_MULTIWORKSPACE; do
  grep -qF "set_env_if_missing .env ${k} 0" "$setup" || fail "setup.sh seeds ${k}=0"
done
grep -qF 'sync_twenty_multiworkspace_bool' "$setup" || fail "setup.sh derives TWENTY_MULTIWORKSPACE_BOOL"
for k in TWENTY_LOCAL_ENABLED TWENTY_MCP_ENABLED TWENTY_MULTIWORKSPACE TWENTY_PORT TWENTY_IMAGE TWENTY_DATA_DIR TWENTY_ENCRYPTION_KEY TWENTY_DB_PASSWORD; do
  grep -qE "^#? ?${k}=" "${ROOT}/.env.example" || fail ".env.example documents ${k}"
done

# Temp files that hold keys while a workspace file is written stay out of git.
for f in compose/twenty/workspaces/.main.env.tmp compose/twenty/workspaces/.main.AbC123 compose/twenty/workspaces/main.env; do
  git -C "$ROOT" check-ignore -q "$f" || fail "$f must be gitignored"
done
if git -C "$ROOT" check-ignore -q compose/twenty/workspaces/.gitkeep; then fail ".gitkeep must stay tracked"; fi

printf 'OK Twenty compose profile\n'
