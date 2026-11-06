#!/bin/sh
# reconcile_twenty_workspaces drops a gone workspace's crm-<slug> entry, its
# TWENTY_<SLUG>_KEY and its tool pins, and leaves everything else alone.
# Runs in the pinned Hermes image, which has the YAML module it needs. Skips
# when Docker or the image is missing.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
image="nousresearch/hermes-agent:${HERMES_AGENT_IMAGE_TAG:-v0.22.7}"

if ! docker image inspect "$image" >/dev/null 2>&1; then
  printf 'skip Twenty reconcile (%s not present)\n' "$image"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
{
  grep -E '^TWENTY_MCP_PINS=' "$bootstrap"
  for fn in twenty_key_env_name reconcile_twenty_workspaces remove_env_key replace_file; do
    range="/^${fn}() {/,/^}/p"
    sed -n "$range" "$bootstrap"
  done
  cat <<'EOF2'
log() { printf '%s\n' "$1"; }
config_path_for() { echo /t/config.yaml; }
caldav_profile_env_file() { echo /t/.env; }
EOF2
} > "${tmp}/fn.sh"

write_fixture() {
  cat > "${tmp}/config.yaml" <<'EOF2'
mcp_servers:
  crm-main:
    url: http://twenty-server:3000/mcp
  crm-old:
    url: https://old.example.com/mcp
  calendar-work:
    url: http://caldav-mcp:8080/mcp
tools:
  tool_search:
    always_include:
      - session_search
      - get_tool_catalog
      - mcp__crm_main__get_tool_catalog
      - mcp__crm_old__get_tool_catalog
      - mcp__crm-old__learn_tools
      - mcp__calendar_work__caldav_get_events
EOF2
  printf 'TWENTY_MAIN_KEY=a\nTWENTY_OLD_KEY=b\nOTHER=1\n' > "${tmp}/.env"
  chmod -R a+rwX "$tmp"
}

run() {
  docker run --rm --entrypoint sh -v "${tmp}:/t" "$image" -c ". /t/fn.sh && reconcile_twenty_workspaces '' $*" > "${tmp}/out" 2>&1 \
    || { cat "${tmp}/out" >&2; printf 'FAIL reconcile exited non-zero\n' >&2; exit 1; }
}

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

# One workspace left: crm-old and its key and pins go, the rest stays.
write_fixture
run main
grep -q '^  crm-old:' "${tmp}/config.yaml" && fail "crm-old entry dropped"
grep -q '^  crm-main:' "${tmp}/config.yaml" || fail "crm-main entry kept"
grep -q '^  calendar-work:' "${tmp}/config.yaml" || fail "a client's entry is never touched"
grep -q 'mcp__crm_old__\|mcp__crm-old__' "${tmp}/config.yaml" && fail "crm-old's pins dropped"
grep -q 'mcp__crm_main__get_tool_catalog' "${tmp}/config.yaml" || fail "crm-main's pins kept"
grep -q -- '- get_tool_catalog' "${tmp}/config.yaml" || fail "bare pins kept while a crm workspace remains"
grep -q 'mcp__calendar_work__caldav_get_events' "${tmp}/config.yaml" || fail "other pins kept"
grep -q '^TWENTY_OLD_KEY=' "${tmp}/.env" && fail "TWENTY_OLD_KEY removed"
grep -q '^TWENTY_MAIN_KEY=a$' "${tmp}/.env" || fail "TWENTY_MAIN_KEY kept"

# None left (TWENTY_MCP_ENABLED=0): every crm pin and the bare Twenty names go.
write_fixture
run
grep -q '^  crm-' "${tmp}/config.yaml" && fail "no crm-* entry remains"
grep -q 'crm' "${tmp}/config.yaml" && fail "no crm pin remains"
grep -q -- '- get_tool_catalog' "${tmp}/config.yaml" && fail "bare Twenty pins dropped with the last workspace"
grep -q -- '- session_search' "${tmp}/config.yaml" || fail "other pins kept"
grep -q '^TWENTY_' "${tmp}/.env" && fail "no TWENTY_ key remains"
grep -q '^OTHER=1$' "${tmp}/.env" || fail "other keys kept"

printf 'OK Twenty reconcile\n'
