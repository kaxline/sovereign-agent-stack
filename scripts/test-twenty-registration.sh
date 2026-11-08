#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# Which Hermes profiles get which Twenty workspace, and with which key.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"

for fn in twenty_key_env_name twenty_read_workspace twenty_key_for_profile twenty_local_off twenty_workspace_slugs register_twenty_mcp_servers; do
  range="/^${fn}() {/,/^}/p"
  # shellcheck disable=SC1090
  eval "$(sed -n "$range" "$bootstrap")"
  command -v "$fn" >/dev/null 2>&1 || { printf 'FAIL %s missing from bootstrap\n' "$fn" >&2; exit 1; }
done
eval "$(grep -E '^TWENTY_(MCP_TIMEOUT|MCP_CONNECT_TIMEOUT|MCP_PINS|LOCAL_MCP_URL|LOCAL_ENABLED)=' "$bootstrap")"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
TWENTY_WORKSPACES_DIR="$tmp/ws"
# The real mkdir; the stub below only replaces it for the bootstrap functions.
# shellcheck disable=SC2218
mkdir -p "$TWENTY_WORKSPACES_DIR"

calls=""
logs=""
log() { logs="${logs}$*
"; }
caldav_profile_env_file() { printf 'env:%s\n' "${1:-default}"; }
mkdir() { :; }
upsert_env() { calls="${calls}upsert:${1}:${2}=${3};"; }
remove_env_key() { calls="${calls}remove:${1}:${2};"; }
write_twenty_mcp_entry() { calls="${calls}write:${1:-default}:${2}:${3}:${4};"; }
pin_mcp_tools() { calls="${calls}pin:${1:-default}:${2}:$(shift 2; echo "$*");"; }
reconcile_twenty_workspaces() { calls="${calls}reconcile:${1:-default}:$(shift; echo "$*");"; }

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

run() {
  calls=""
  logs=""
  register_twenty_mcp_servers "$1"
}

has() {
  case "$calls" in *"$1"*) return 0 ;; *) return 1 ;; esac
}

PINS="get_tool_catalog learn_tools execute_tool list_object_metadata_names list_skills load_skills search_help_center"
[ "$TWENTY_MCP_PINS" = "$PINS" ] || fail "TWENTY_MCP_PINS is the seven Twenty MCP tools"

[ "$(twenty_key_env_name main)" = "TWENTY_MAIN_KEY" ] || fail "main -> TWENTY_MAIN_KEY"
[ "$(twenty_key_env_name sales-eu)" = "TWENTY_SALES_EU_KEY" ] || fail "sales-eu -> TWENTY_SALES_EU_KEY"

cat > "$TWENTY_WORKSPACES_DIR/main.env" <<'EOF'
# local workspace
TWENTY_URL=http://twenty-server:3000
TWENTY_WORKSPACE_URL=http://localhost:3000/
TWENTY_KEY_RO=ro-secret-main
TWENTY_KEY_RW="rw-secret-main"
EOF
cat > "$TWENTY_WORKSPACES_DIR/remote.env" <<'EOF'
TWENTY_URL=https://crm.example.com/
TWENTY_KEY_RW=rw-secret-remote
TWENTY_KEY_RO=
EOF
printf 'TWENTY_URL=http://twenty-server:3000\nTWENTY_KEY_RW=rw-secret-bad\n' > "$TWENTY_WORKSPACES_DIR/Bad_Slug.env"
printf 'TWENTY_URL=http://twenty-server:3000\nTWENTY_KEY_RW=rw-secret-ex\n' > "$TWENTY_WORKSPACES_DIR/sample.env.example"
printf 'TWENTY_URL=http://twenty-server:3000\nTWENTY_KEY_RW=rw-secret-dot\n' > "$TWENTY_WORKSPACES_DIR/.hidden.env"
printf 'TWENTY_URL=ftp://crm.example.com\nTWENTY_KEY_RW=rw-secret-ftp\n' > "$TWENTY_WORKSPACES_DIR/ftp.env"
printf 'TWENTY_URL=https://crm.example.com/mcp\nTWENTY_KEY_RW=rw-secret-mcp\n' > "$TWENTY_WORKSPACES_DIR/withmcp.env"

TWENTY_MCP_ENABLED=1
for profile in "" browser; do
  run "$profile"
  p="${profile:-default}"
  has "upsert:env:${p}:TWENTY_MAIN_KEY=rw-secret-main;" || fail "${p}: main gets the read/write key ($calls)"
  has "write:${p}:crm-main:http://twenty-server:3000/mcp:TWENTY_MAIN_KEY;" || fail "${p}: crm-main at twenty-server ($calls)"
  has "pin:${p}:crm-main:${PINS};" || fail "${p}: crm-main pins the seven tools"
  has "upsert:env:${p}:TWENTY_REMOTE_KEY=rw-secret-remote;" || fail "${p}: remote gets its read/write key"
  has "write:${p}:crm-remote:https://crm.example.com/mcp:TWENTY_REMOTE_KEY;" || fail "${p}: trailing slash stripped ($calls)"
  has "write:${p}:crm-withmcp:https://crm.example.com/mcp:TWENTY_WITHMCP_KEY;" || fail "${p}: a URL already ending in /mcp is used as is"
  has "reconcile:${p}:main remote withmcp;" || fail "${p}: reconcile keeps main remote withmcp ($calls)"
done

run api-server
has "upsert:env:api-server:TWENTY_MAIN_KEY=ro-secret-main;" || fail "api-server: main gets the read-only key ($calls)"
has "write:api-server:crm-main:http://twenty-server:3000/mcp:TWENTY_MAIN_KEY;" || fail "api-server: crm-main registered"
if has "crm-remote"; then fail "api-server: remote has no read-only key, so no entry ($calls)"; fi
if has "rw-secret"; then fail "api-server must never receive a read/write key ($calls)"; fi
has "reconcile:api-server:main;" || fail "api-server: reconcile keeps only main, dropping any old crm-remote ($calls)"

# TWENTY_ACCESS picks the interactive profiles' key; api-server stays read-only.
cat > "$TWENTY_WORKSPACES_DIR/lookup.env" <<'EOF2'
TWENTY_URL=http://twenty-server:3000
TWENTY_ACCESS=read
TWENTY_KEY_RO=ro-secret-lookup
TWENTY_KEY_RW=rw-secret-lookup
EOF2
cat > "$TWENTY_WORKSPACES_DIR/rdonly.env" <<'EOF2'
TWENTY_URL=http://twenty-server:3000
TWENTY_ACCESS=read
TWENTY_KEY_RW=rw-secret-rdonly
EOF2
cat > "$TWENTY_WORKSPACES_DIR/odd.env" <<'EOF2'
TWENTY_URL=http://twenty-server:3000
TWENTY_ACCESS=admin
TWENTY_KEY_RW=rw-secret-odd
TWENTY_KEY_RO=ro-secret-odd
EOF2
run browser
has "upsert:env:browser:TWENTY_LOOKUP_KEY=ro-secret-lookup;" || fail "TWENTY_ACCESS=read gives browser the read-only key ($calls)"
if has "rw-secret-lookup"; then fail "TWENTY_ACCESS=read must never hand out the read/write key"; fi
if has "crm-rdonly"; then fail "TWENTY_ACCESS=read with no read-only key gives no entry ($calls)"; fi
case "$logs" in *"rdonly"*TWENTY_KEY_RO*) ;; *) fail "the missing read-only key is warned about" ;; esac
if has "crm-odd"; then fail "an unknown TWENTY_ACCESS gives no entry ($calls)"; fi
case "$logs" in *"odd"*TWENTY_ACCESS*) ;; *) fail "an unknown TWENTY_ACCESS is warned about" ;; esac
run api-server
has "upsert:env:api-server:TWENTY_LOOKUP_KEY=ro-secret-lookup;" || fail "api-server keeps the read-only key with TWENTY_ACCESS=read"
[ "$(twenty_workspace_slugs browser | tr '\n' ' ')" = "lookup main remote withmcp " ] || fail "browser slugs with TWENTY_ACCESS ($(twenty_workspace_slugs browser | tr '\n' ' '))"
rm "$TWENTY_WORKSPACES_DIR/lookup.env" "$TWENTY_WORKSPACES_DIR/rdonly.env" "$TWENTY_WORKSPACES_DIR/odd.env"

# Skipped files.
run ""
for bad in Bad_Slug sample hidden ftp; do
  if has "crm-${bad}" || has "crm-.${bad}"; then fail "${bad} must be skipped ($calls)"; fi
done
case "$logs" in *"Bad_Slug.env"*) ;; *) fail "a bad slug is warned about" ;; esac
case "$logs" in *"ftp.env"*) ;; *) fail "a non-http URL is warned about" ;; esac
case "$logs" in *secret*) fail "log output must not contain a key" ;; esac

# Slugs registered per profile, for the eager-tool pins.
[ "$(twenty_workspace_slugs "" | tr '\n' ' ')" = "main remote withmcp " ] || fail "default slugs"
[ "$(twenty_workspace_slugs api-server | tr '\n' ' ')" = "main " ] || fail "api-server slugs"

# Local instance off (CR 0023 R2): local files skipped and dropped, remote kept.
[ "$TWENTY_LOCAL_ENABLED" = 1 ] || fail "unset TWENTY_LOCAL_ENABLED keeps registering local workspaces"
TWENTY_LOCAL_ENABLED=0
for profile in "" api-server; do
  run "$profile"
  p="${profile:-default}"
  if has "crm-main"; then fail "${p}: local off gives no crm-main ($calls)"; fi
  if has "TWENTY_MAIN_KEY"; then fail "${p}: local off writes no TWENTY_MAIN_KEY ($calls)"; fi
  case "$logs" in *"'main'"*"local instance is off"*) ;; *) fail "${p}: the local skip is logged ($logs)" ;; esac
done
run ""
has "write:default:crm-remote:" || fail "local off keeps the remote workspace ($calls)"
has "reconcile:default:remote withmcp;" || fail "local off: reconcile drops crm-main ($calls)"
[ "$(twenty_workspace_slugs "" | tr '\n' ' ')" = "remote withmcp " ] || fail "local off: default slugs"
[ -z "$(twenty_workspace_slugs api-server)" ] || fail "local off: no api-server slugs"
TWENTY_LOCAL_ENABLED=1
run ""
has "write:default:crm-main:" || fail "local back on restores crm-main ($calls)"

# Disabled: everything of ours goes.
TWENTY_MCP_ENABLED=0
run ""
[ "$calls" = "reconcile:default:;" ] || fail "disabled: reconcile with no slugs, nothing written ($calls)"
[ -z "$(twenty_workspace_slugs "")" ] || fail "disabled: no slugs to pin"

# Missing directory with the flag on: warn, drop ours.
TWENTY_MCP_ENABLED=1
TWENTY_WORKSPACES_DIR="$tmp/none"
run ""
[ "$calls" = "reconcile:default:;" ] || fail "missing dir: reconcile with no slugs ($calls)"

printf 'OK Twenty MCP registration\n'
