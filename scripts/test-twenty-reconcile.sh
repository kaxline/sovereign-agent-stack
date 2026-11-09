#!/bin/sh
# register_addon_instance_servers end to end for Twenty: writes crm-<slug>
# with a Bearer header that references CRM_<SLUG>_KEY, and its reconcile drops
# a gone workspace's entry, key and pins, removes the pre-0.3.13
# TWENTY_<SLUG>_KEY lines, and leaves everything else alone. Uses the real
# addons/twenty/addon.json and a synthetic store. Runs in the pinned Hermes
# image, which has the YAML module the writers need. Skips when Docker or the
# image is missing.
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
  for fn in addon_instances_plan addon_instance_key_env addon_instances_warn_none \
      register_addon_instance_servers write_bearer_mcp_entry reconcile_addon_instance_servers \
      remove_env_key replace_file upsert_env pin_mcp_tools mcp_tool_name \
      pin_tool_search_always_include ensure_yaml_list_items; do
    range="/^${fn}() {/,/^}/p"
    sed -n "$range" "$bootstrap"
  done
  cat <<'EOF2'
log() { printf '%s\n' "$1"; }
config_path_for() { echo /t/config.yaml; }
caldav_profile_env_file() { echo /t/.env; }
ADDONS_DIR=/addons
ADDON_MANIFEST_PY=/addon_manifest.py
ADDON_DATA_DIR=/t/addon-data
EOF2
} > "${tmp}/fn.sh"

store="${tmp}/addon-data/twenty"
mkdir -p "${store}/instances" "${store}/secrets"
printf '{"url": "http://twenty-server:3000", "access": "write", "local": true}\n' > "${store}/instances/main.json"
printf 'rw-fake-main' > "${store}/secrets/main.key_rw"
printf 'ro-fake-main' > "${store}/secrets/main.key_ro"

write_fixture() {
  cat > "${tmp}/config.yaml" <<'EOF2'
mcp_servers:
  crm-main:
    url: http://twenty-server:3000/mcp
    headers:
      Authorization: Bearer ${TWENTY_MAIN_KEY}
  crm-old:
    url: https://old.example.com/mcp
  calendar-work:
    url: http://caldav-mcp:8080/mcp
  my-crm:
    url: http://twenty-server:3000/mcp
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
  printf 'TWENTY_MAIN_KEY=a\nTWENTY_OLD_KEY=b\nCRM_OLD_KEY=c\nTWENTY_ENCRYPTION_KEY=d\nOTHER=1\n' > "${tmp}/.env"
  chmod -R a+rwX "$tmp"
}

# run <TWENTY_MCP_ENABLED>
run() {
  docker run --rm --entrypoint sh -v "${tmp}:/t" -v "${ROOT}/addons:/addons:ro" \
    -v "${ROOT}/scripts/lib/addon_manifest.py:/addon_manifest.py:ro" \
    -e TWENTY_MCP_ENABLED="$1" -e TWENTY_LOCAL_ENABLED=1 "$image" \
    -c ". /t/fn.sh && register_addon_instance_servers ''" > "${tmp}/out" 2>&1 \
    || { cat "${tmp}/out" >&2; printf 'FAIL register exited non-zero\n' >&2; exit 1; }
}

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

no_key_leaks() {
  if grep -q 'fake-main' "${tmp}/out" "${tmp}/config.yaml"; then fail "$1: a key reached the log or config.yaml"; fi
}

# One workspace left: crm-main is rewritten, crm-old and its key and pins go,
# the rest stays.
write_fixture
run 1
grep -q '^  crm-old:' "${tmp}/config.yaml" && fail "crm-old entry dropped"
grep -q '^  crm-main:' "${tmp}/config.yaml" || fail "crm-main entry kept"
# shellcheck disable=SC2016 # the literal reference
grep -qF 'Authorization: Bearer ${CRM_MAIN_KEY}' "${tmp}/config.yaml" || fail "crm-main's header references CRM_MAIN_KEY"
grep -q 'TWENTY_MAIN_KEY' "${tmp}/config.yaml" && fail "the old header reference is replaced"
grep -q '^  calendar-work:' "${tmp}/config.yaml" || fail "a client's entry is never touched"
grep -q '^  my-crm:' "${tmp}/config.yaml" || fail "a client's entry at the same URL stays"
grep -q 'mcp__crm_old__\|mcp__crm-old__' "${tmp}/config.yaml" && fail "crm-old's pins dropped"
grep -q 'mcp__crm_main__search_help_center' "${tmp}/config.yaml" || fail "crm-main's pins written"
grep -q -- '- get_tool_catalog' "${tmp}/config.yaml" || fail "bare pins kept while a crm workspace remains"
grep -q 'mcp__calendar_work__caldav_get_events' "${tmp}/config.yaml" || fail "other pins kept"
grep -q '^CRM_MAIN_KEY=rw-fake-main$' "${tmp}/.env" || fail "CRM_MAIN_KEY holds the read/write key"
grep -q '^CRM_OLD_KEY=' "${tmp}/.env" && fail "CRM_OLD_KEY removed"
grep -q '^TWENTY_MAIN_KEY=' "${tmp}/.env" && fail "legacy TWENTY_MAIN_KEY removed"
grep -q '^TWENTY_OLD_KEY=' "${tmp}/.env" && fail "legacy TWENTY_OLD_KEY removed"
grep -q '^TWENTY_ENCRYPTION_KEY=d$' "${tmp}/.env" || fail "TWENTY_ENCRYPTION_KEY is not a workspace key and stays"
grep -q '^OTHER=1$' "${tmp}/.env" || fail "other keys kept"
no_key_leaks "enabled"

# The workspace is removed from the store: its entry, key and pins go.
mv "${store}/instances/main.json" "${tmp}/main.json.away"
run 1
grep -q '^  crm-' "${tmp}/config.yaml" && fail "removed workspace: no crm-* entry remains"
grep -q 'mcp__crm' "${tmp}/config.yaml" && fail "removed workspace: no crm pin remains"
grep -q -- '- get_tool_catalog' "${tmp}/config.yaml" && fail "removed workspace: bare Twenty pins dropped with the last one"
grep -q '^CRM_MAIN_KEY=' "${tmp}/.env" && fail "removed workspace: CRM_MAIN_KEY removed"
mv "${tmp}/main.json.away" "${store}/instances/main.json"

# TWENTY_MCP_ENABLED=0: every crm-* entry, key and pin goes, bare names too.
write_fixture
run 0
grep -q '^  crm-' "${tmp}/config.yaml" && fail "disabled: no crm-* entry remains"
grep -q 'mcp__crm' "${tmp}/config.yaml" && fail "disabled: no crm pin remains"
grep -q -- '- get_tool_catalog' "${tmp}/config.yaml" && fail "disabled: bare Twenty pins dropped"
grep -q -- '- session_search' "${tmp}/config.yaml" || fail "disabled: other pins kept"
grep -q '^  my-crm:' "${tmp}/config.yaml" || fail "disabled: a client's entry stays"
grep -q '^CRM_\|^TWENTY_MAIN_KEY\|^TWENTY_OLD_KEY' "${tmp}/.env" && fail "disabled: no workspace key remains"
grep -q '^OTHER=1$' "${tmp}/.env" || fail "disabled: other keys kept"
no_key_leaks "disabled"

printf 'OK Twenty reconcile\n'
