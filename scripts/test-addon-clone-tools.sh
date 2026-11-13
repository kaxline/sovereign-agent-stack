#!/bin/sh
# browser and api-server are cloned from default at a fresh install, so they
# start with default's add-on MCP entries, tools.include and all. The add-on
# registration must leave each profile with exactly the tools its manifest
# access gives it: Messages registers browser "ro", and a browser that kept
# default's list would hold messages_triage_batch.
#
#   sh scripts/test-addon-clone-tools.sh [--host-only | --image]
#
# The image part runs register_addon_mcp_servers and
# register_addon_instance_servers in nousresearch/hermes-agent (Hermes's YAML
# writer lives there) with every add-on flag on and a synthetic Actual budget
# bound rw on default and ro elsewhere: default first, then a copy of its
# config as the cloned profile, then a second run on both.
# It is skipped when Docker or the image is missing unless --image or
# HERMES_IMAGE_REQUIRED=1 is given.
set -eu

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
BOOTSTRAP=compose/hermes/bootstrap-api-profile.sh
# shellcheck source=lib/hermes-image.sh
. scripts/lib/hermes-image.sh
IMAGE="$HERMES_IMAGE"

# Host wiring: register_mcp_server writes the allowlist exactly.
# shellcheck disable=SC2016 # the literal bootstrap line
grep -q 'set_yaml_list --exact "$(config_path_for "$_rms_profile")" "mcp_servers.${_rms_key}.tools.include"' "$BOOTSTRAP" \
  || fail "register_mcp_server must write tools.include with set_yaml_list --exact"
echo "ok: host wiring"

hermes_image_gate "${1:-}" "image checks"

work="$(mktemp -d "${TMPDIR:-/tmp}/addon-clone-tools.XXXXXX")"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/home" "$work/addon-data/actual/instances"
printf '{"server": "local", "sync_id": "s1", "bind": {"default": "rw", "browser": "ro", "api-server": "ro"}}\n' \
  > "$work/addon-data/actual/instances/home.json"
for fn in addon_mcp_plan register_addon_mcp_servers register_mcp_server set_yaml_list \
    drop_mcp_entries_for_url write_bearer_mcp_entry write_headers_mcp_entry config_path_for \
    addon_instances_plan register_addon_instance_servers addon_instances_warn_none \
    reconcile_addon_instance_servers addon_instance_key_env; do
  fn_source "$BOOTSTRAP" "$fn"
done > "$work/fns.sh"

# Every built-in add-on flag on. Add-ons with mcp.auth stay without a key, so
# they skip; their entries carry no tools.include anyway.
flags="$(python3 - <<'PY'
import json
from pathlib import Path
for p in sorted(Path("addons").glob("*/addon.json")):
    flag = (json.loads(p.read_text()).get("builtin") or {}).get("flag")
    if flag:
        print(f"{flag}=1")
PY
)"
printf '%s\n' "$flags" > "$work/flags.env"

cat > "$work/run.sh" <<'EOF'
set -eu
# Files made here are root's; on Linux the runner couldn't delete them.
trap 'chown -R "$HOST_IDS" /w' EXIT
log() { :; }
hermes_config_set() { :; }
pin_mcp_tools() { :; }
profile_env_file() { echo /h/.env; }
remove_env_key() { :; }
upsert_env() { :; }
HERMES_HOME=/h
ADDONS_DIR=/bootstrap/addons
ADDON_MANIFEST_PY=/bootstrap/addon_manifest.py
ADDON_DATA_DIR=/w/addon-data
set -a
. /w/flags.env
set +a
. /w/fns.sh

# What each profile's add-on entries should allow, from the plan.
want_lines() {
  addon_mcp_plan "$1" | while IFS='|' read -r name _t on access _u _to _c _tr tools _p _a keysrc _rest; do
    if [ "$on" = 1 ] && [ -n "$access" ] && [ -z "$keysrc" ] && [ -n "$tools" ]; then printf '%s=%s\n' "$name" "$tools"; fi
  done
  addon_instances_plan "$1" | while IFS='|' read -r name _t on _u _k _kf _to _c _p _r _h _s tools _rest; do
    if [ "$on" = 1 ] && [ -n "$tools" ]; then printf '%s=%s\n' "$name" "$tools"; fi
  done
}
want() { want_lines "$1" | sort; }
register() {
  register_addon_mcp_servers "$1"
  register_addon_instance_servers "$1"
}
# What the profile's config.yaml allows, for the add-on entries in want.
have() {
  python3 - "$(config_path_for "$1")" "$2" <<'PY'
import sys
sys.path.insert(0, "/opt/hermes")
import hermes_yaml as y
cfg = y.safe_load(open(sys.argv[1])) or {}
servers = cfg.get("mcp_servers") or {}
for line in sys.argv[2].splitlines():
    name = line.split("=", 1)[0]
    inc = ((servers.get(name) or {}).get("tools") or {}).get("include")
    print(f"{name}={' '.join(map(str, inc or []))}")
PY
}

printf "{}\n" > /h/config.yaml
register ""
want_default="$(want "")"
[ "$(have "" "$want_default")" = "$want_default" ] && echo "default-fresh"
default_before="$(cat /h/config.yaml)"
for p in browser api-server; do
  mkdir -p "/h/profiles/$p"
  cp /h/config.yaml "/h/profiles/$p/config.yaml"
  register "$p"
  w="$(want "$p")"
  h="$(have "$p" "$w")"
  if [ "$h" = "$w" ]; then echo "${p}-clone"; else printf '%s-clone-diff\nwant:\n%s\nhave:\n%s\n' "$p" "$w" "$h"; fi
  register "$p"
  [ "$(have "$p" "$w")" = "$w" ] && echo "${p}-rerun"
done
register ""
[ "$(cat /h/config.yaml)" = "$default_before" ] && echo "default-rerun"
# messages on browser: the read tools, never the triage batch.
have browser "messages=" | grep -q messages_triage_batch || echo "browser-no-triage"
# The budget: every tool on default, the read tools on browser.
have "" "budget-home=" | grep -q create-transaction && echo "default-budget-rw"
have browser "budget-home=" | grep -q 'budget-home=get-' && echo "browser-budget-present"
have browser "budget-home=" | grep -q create-transaction || echo "browser-budget-ro"
EOF

out="$(docker run --rm --entrypoint /bin/sh -e HOST_IDS="$(id -u):$(id -g)" \
  -v "$ROOT/addons:/bootstrap/addons:ro" \
  -v "$ROOT/scripts/lib/addon_manifest.py:/bootstrap/addon_manifest.py:ro" \
  -v "$work:/w" -v "$work/home:/h" "$IMAGE" ${TRACE:+-x} /w/run.sh 2>&1)" || fail "registration run failed: $out"

expect() { printf '%s\n' "$out" | grep -qx -- "$1" || fail "$2 (output: $out)"; }
expect default-fresh "default gets each add-on's rw list on a fresh install"
expect browser-clone "browser ends up with exactly its manifest tools after the clone"
expect browser-rerun "a second bootstrap leaves browser's lists exact"
expect api-server-clone "api-server ends up with exactly its manifest tools after the clone"
expect api-server-rerun "a second bootstrap leaves api-server's lists exact"
expect default-rerun "a second bootstrap leaves default's config unchanged"
expect browser-no-triage "browser must not hold messages_triage_batch"
expect default-budget-rw "a budget bound rw on default gets the write tools there"
expect browser-budget-present "a budget bound ro on browser registers there"
expect browser-budget-ro "a budget bound ro on browser must not keep default's write tools"
echo "OK add-on tool lists on cloned profiles"
