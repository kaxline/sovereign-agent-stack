#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# shellcheck disable=SC2218  # the real mkdir runs before the stub below replaces it for the bootstrap functions
# Which Hermes profiles get which Twenty workspace, and with which key: the
# bootstrap's generic mcp.instances registration, run on the host against the
# real addons/twenty/addon.json and a synthetic instance store. The YAML
# writers are stubbed; test-twenty-reconcile.sh runs them in the Hermes image.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"

load_fns "$bootstrap" addon_instances_plan addon_instance_key_env addon_instance_urls \
  addon_instance_pin_args addon_instances_warn_none register_addon_instance_servers mcp_tool_name

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
# Only Twenty's manifest: other instance add-ons (Actual) add their own prefixes.
ADDONS_DIR="${tmp}/addons"
mkdir -p "${ADDONS_DIR}/twenty"
cp "${ROOT}/addons/twenty/addon.json" "${ADDONS_DIR}/twenty/addon.json"
ADDON_MANIFEST_PY="${ROOT}/scripts/lib/addon_manifest.py"
ADDON_DATA_DIR="${tmp}/addon-data"
store="${ADDON_DATA_DIR}/twenty"
# The real mkdir; the stub below only replaces it for the bootstrap functions.
# shellcheck disable=SC2218
mkdir -p "${store}/instances" "${store}/secrets"

calls=""
logs=""
log() { logs="${logs}$*
"; }
caldav_profile_env_file() { printf 'env:%s\n' "${1:-default}"; }
mkdir() { :; }
upsert_env() { calls="${calls}upsert:${1}:${2}=${3};"; }
remove_env_key() { calls="${calls}remove:${1}:${2};"; }
write_bearer_mcp_entry() { calls="${calls}write:${1:-default}:${2}:${3}:${4}:${5}:${6}:${7}:${8};"; }
pin_mcp_tools() { calls="${calls}pin:${1:-default}:${2}:$(shift 2; echo "$*");"; }
reconcile_addon_instance_servers() { calls="${calls}reconcile:${1:-default}:$(shift; echo "$*");"; }

run() {
  calls=""
  logs=""
  register_addon_instance_servers "$1"
}

has() {
  case "$calls" in *"$1"*) return 0 ;; *) return 1 ;; esac
}

# logged <basic regex>: a single log line matches.
logged() { printf '%s' "$logs" | grep -q -- "$1"; }

# instance <slug> <json>; key <slug>.<key_ro|key_rw> <value>
instance() { printf '%s\n' "$2" > "${store}/instances/$1.json"; }
key() { printf '%s' "$2" > "${store}/secrets/$1"; }

PINS="get_tool_catalog learn_tools execute_tool list_object_metadata_names list_skills load_skills search_help_center"
SPEC="crm-=$(printf '%s' "$PINS" | tr ' ' ',')"

[ "$(addon_instance_key_env crm-main)" = "CRM_MAIN_KEY" ] || fail "crm-main -> CRM_MAIN_KEY"
[ "$(addon_instance_key_env crm-sales-eu)" = "CRM_SALES_EU_KEY" ] || fail "crm-sales-eu -> CRM_SALES_EU_KEY"

instance main '{"url": "http://twenty-server:3000", "access": "write", "local": true, "workspace_url": "http://localhost:3000/"}'
key main.key_ro ro-secret-main
key main.key_rw rw-secret-main
instance remote '{"url": "https://crm.example.com/", "access": "write", "local": false}'
key remote.key_rw rw-secret-remote
instance Bad_Slug '{"url": "http://twenty-server:3000", "access": "write"}'
key Bad_Slug.key_rw rw-secret-bad
instance .hidden '{"url": "http://twenty-server:3000", "access": "write"}'
key .hidden.key_rw rw-secret-dot
printf '{}\n' > "${store}/instances/sample.json.example"
instance ftp '{"url": "ftp://crm.example.com", "access": "write"}'
key ftp.key_rw rw-secret-ftp
instance withmcp '{"url": "https://crm.example.com/mcp", "access": "write"}'
key withmcp.key_rw rw-secret-withmcp
instance creds '{"url": "https://user:pw-secret-creds@crm.example.com", "access": "write"}'
key creds.key_rw rw-secret-creds

export TWENTY_MCP_ENABLED=1 TWENTY_LOCAL_ENABLED=1
unset TWENTY_MCP_TIMEOUT TWENTY_MCP_CONNECT_TIMEOUT 2>/dev/null || true
for profile in "" browser; do
  run "$profile"
  p="${profile:-default}"
  has "upsert:env:${p}:CRM_MAIN_KEY=rw-secret-main;" || fail "${p}: main gets the read/write key ($calls)"
  has "write:${p}:crm-main:http://twenty-server:3000/mcp:CRM_MAIN_KEY:60:30:Authorization:Bearer;" \
    || fail "${p}: crm-main at twenty-server with a Bearer header from CRM_MAIN_KEY ($calls)"
  has "pin:${p}:crm-main:${PINS};" || fail "${p}: crm-main pins the seven tools"
  has "upsert:env:${p}:CRM_REMOTE_KEY=rw-secret-remote;" || fail "${p}: remote gets its read/write key"
  has "write:${p}:crm-remote:https://crm.example.com/mcp:CRM_REMOTE_KEY:" || fail "${p}: trailing slash stripped ($calls)"
  has "write:${p}:crm-withmcp:https://crm.example.com/mcp:CRM_WITHMCP_KEY:" \
    || fail "${p}: a URL already ending in /mcp is used as is ($calls)"
  has "reconcile:${p}:${SPEC} crm-main crm-remote crm-withmcp;" || fail "${p}: reconcile keeps main remote withmcp ($calls)"
done

run api-server
has "upsert:env:api-server:CRM_MAIN_KEY=ro-secret-main;" || fail "api-server: main gets the read-only key ($calls)"
has "write:api-server:crm-main:http://twenty-server:3000/mcp:CRM_MAIN_KEY:" || fail "api-server: crm-main registered"
if has "crm-remote:"; then fail "api-server: remote has no read-only key, so no entry ($calls)"; fi
if has "rw-secret"; then fail "api-server must never receive a read/write key ($calls)"; fi
has "reconcile:api-server:${SPEC} crm-main;" || fail "api-server: reconcile keeps only crm-main, dropping any old crm-remote ($calls)"
if logged "WARNING.*'crm-remote'"; then fail "api-server: a missing read-only key is not a warning ($logs)"; fi
logged "'crm-remote'.*key_ro" || fail "api-server: the skipped remote is logged ($logs)"

# access picks the interactive profiles' key; api-server stays read-only.
instance lookup '{"url": "http://twenty-server:3000", "access": "read", "local": true}'
key lookup.key_ro ro-secret-lookup
key lookup.key_rw rw-secret-lookup
instance rdonly '{"url": "http://twenty-server:3000", "access": "read", "local": true}'
key rdonly.key_rw rw-secret-rdonly
instance odd '{"url": "http://twenty-server:3000", "access": "admin", "local": true}'
key odd.key_rw rw-secret-odd
key odd.key_ro ro-secret-odd
run browser
has "upsert:env:browser:CRM_LOOKUP_KEY=ro-secret-lookup;" || fail "access read gives browser the read-only key ($calls)"
if has "rw-secret-lookup"; then fail "access read must never hand out the read/write key"; fi
if has "crm-rdonly:"; then fail "access read with no read-only key gives no entry ($calls)"; fi
logged "WARNING.*'crm-rdonly'.*key_ro" || fail "the missing read-only key is warned about ($logs)"
if has "crm-odd:"; then fail "an unknown access gives no entry ($calls)"; fi
logged "WARNING.*'crm-odd'.*access" || fail "an unknown access is warned about ($logs)"
case "$logs" in *admin*) fail "the log must not echo the unknown access value" ;; esac
run api-server
has "upsert:env:api-server:CRM_LOOKUP_KEY=ro-secret-lookup;" || fail "api-server keeps the read-only key with access read"
if has "crm-odd:"; then fail "api-server: an unknown access gives no entry either ($calls)"; fi
[ "$(addon_instance_urls browser | sort -u | tr '\n' ' ')" = "http://twenty-server:3000/mcp https://crm.example.com/mcp " ] \
  || fail "instance URLs for the parked retry ($(addon_instance_urls browser | tr '\n' ' '))"
rm "${store}/instances/lookup.json" "${store}/instances/rdonly.json" "${store}/instances/odd.json"

# Skipped files.
run "" 2> "$tmp/stderr"
for bad in Bad_Slug sample hidden .hidden ftp creds; do
  if has "crm-${bad}:"; then fail "${bad} must be skipped ($calls)"; fi
done
grep -q 'Bad_Slug.json' "$tmp/stderr" || fail "a bad slug is warned about ($(cat "$tmp/stderr"))"
logged "WARNING.*'crm-creds'.*user name or password" || fail "a URL with credentials is warned about ($logs)"
if printf '%s' "$logs" | grep -q 'pw-secret'; then fail "the log must not echo a URL's password"; fi
logged "WARNING.*'crm-ftp'.*http" || fail "a non-http URL is warned about ($logs)"
case "$logs" in *-secret-*) fail "log output must not contain a key" ;; esac
plan="$(env TWENTY_MCP_ENABLED=1 TWENTY_LOCAL_ENABLED=1 python3 "$ADDON_MANIFEST_PY" instances-plan "$ADDONS_DIR" "$ADDON_DATA_DIR" default 2>&1)"
case "$plan" in *-secret-*) fail "instances-plan output must not contain a key" ;; esac

# Pins per profile, for reconcile_stack_eager_tools.
pins_default="$(addon_instance_pin_args "")"
case "$pins_default" in *" get_tool_catalog mcp__crm_main__get_tool_catalog"*) ;; *) fail "default pins crm-main ($pins_default)" ;; esac
case "$pins_default" in *"mcp__crm_remote__search_help_center"*) ;; *) fail "default pins crm-remote" ;; esac
case "$(addon_instance_pin_args api-server)" in *crm_remote*) fail "api-server does not pin crm-remote" ;; esac

rm "${store}/instances/ftp.json" "${store}/instances/withmcp.json" "${store}/instances/creds.json"

# Local instance off (CR 0023 R2): local instances skipped and dropped, remote kept.
TWENTY_LOCAL_ENABLED=0
for profile in "" api-server; do
  run "$profile"
  p="${profile:-default}"
  if has "crm-main:"; then fail "${p}: local off gives no crm-main ($calls)"; fi
  if has "CRM_MAIN_KEY"; then fail "${p}: local off writes no CRM_MAIN_KEY ($calls)"; fi
  logged "'crm-main'.*TWENTY_LOCAL_ENABLED is off" || fail "${p}: the local skip is logged ($logs)"
done
# api-server had only main: nothing registers, and that's expected, not a warning.
case "$logs" in *"no instance registers"*) fail "local off: no warning when only local instances were skipped ($logs)" ;; esac
run ""
has "write:default:crm-remote:" || fail "local off keeps the remote workspace ($calls)"
has "reconcile:default:${SPEC} crm-remote;" || fail "local off: reconcile drops crm-main ($calls)"
case "$(addon_instance_pin_args "")" in *crm_main*) fail "local off: crm-main is not pinned" ;; esac
TWENTY_LOCAL_ENABLED=1
run ""
has "write:default:crm-main:" || fail "local back on restores crm-main ($calls)"

# A remote-only profile that registers nothing warns.
rm "${store}/secrets/remote.key_rw"
TWENTY_LOCAL_ENABLED=0
run ""
case "$logs" in *"WARNING: Twenty CRM is on but no instance registers on 'default'"*) ;; *) fail "nothing registers: warned ($logs)" ;; esac
key remote.key_rw rw-secret-remote
TWENTY_LOCAL_ENABLED=1

# Timeouts from the manifest's env names.
export TWENTY_MCP_TIMEOUT=90 TWENTY_MCP_CONNECT_TIMEOUT=10
run ""
unset TWENTY_MCP_TIMEOUT TWENTY_MCP_CONNECT_TIMEOUT
has "write:default:crm-main:http://twenty-server:3000/mcp:CRM_MAIN_KEY:90:10:" || fail "TWENTY_MCP_TIMEOUT and TWENTY_MCP_CONNECT_TIMEOUT apply ($calls)"

# Disabled: nothing written, every crm-* entry dropped by the reconcile.
TWENTY_MCP_ENABLED=0
run ""
[ "$calls" = "reconcile:default:${SPEC};" ] || fail "disabled: reconcile with no names, nothing written ($calls)"
case "$logs" in *"Skipping Twenty CRM MCP registration (TWENTY_MCP_ENABLED is off)"*) ;; *) fail "disabled: logged ($logs)" ;; esac
[ -z "$(addon_instance_pin_args "")" ] || fail "disabled: nothing to pin"

# No store (nothing connected) with the flag on: drop ours and warn.
TWENTY_MCP_ENABLED=1
ADDON_DATA_DIR="$tmp/none"
run ""
[ "$calls" = "reconcile:default:${SPEC};" ] || fail "missing store: reconcile with no names ($calls)"
case "$logs" in *"no instance registers"*) ;; *) fail "missing store: warned ($logs)" ;; esac

# A plan that fails leaves the entries alone.
ADDON_MANIFEST_PY="$tmp/missing.py"
run "" 2>/dev/null
[ -z "$calls" ] || fail "failed plan: nothing written or reconciled ($calls)"

printf 'OK Twenty MCP registration\n'
