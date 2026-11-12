#!/bin/sh
# The bootstrap's cron fence (fence_mail_from_cron) keeps the browser, and
# add-ons whose manifest doesn't set permissions.cron true, out of
# platform_toolsets.cron.
#
#   sh scripts/test-addon-cron-fence.sh [--host-only | --image]
#
# The host part checks the wiring. The image part runs the real function in
# nousresearch/hermes-agent (Hermes's YAML writer lives there) against scratch
# configs; it is skipped when Docker or the image is missing unless --image
# or HERMES_IMAGE_REQUIRED=1 is given.
set -eu

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
BOOTSTRAP=compose/hermes/bootstrap-api-profile.sh
# shellcheck source=lib/hermes-image.sh
. scripts/lib/hermes-image.sh
IMAGE="$HERMES_IMAGE"

# shellcheck disable=SC2016 # the literal bootstrap line
grep -q '_out="$(python3 - "$_fmc_cfg" "$ADDON_MANIFEST_PY" "$ADDONS_DIR" "$ADDON_DATA_DIR" <<' "$BOOTSTRAP" \
  || fail "fence_mail_from_cron must pass the add-on manifests and instance stores to its python"
grep -q 'addon_manifest.cron_fence(Path(sys.argv\[3\]), data_dir=Path(sys.argv\[4\]))' "$BOOTSTRAP" \
  || fail "the fence reads instance names from the stores"
# A connected Actual budget's write entry sits at a path under the fenced rw
# URL's host (http://actual-mcp-rw:3000/<slug>/mcp), so the fence compares
# scheme, host and port.
grep -q 'origin(str(servers\[n\].get("url", ""))) in addon_origins' "$BOOTSTRAP" \
  || fail "the fence matches add-on URLs by scheme, host and port"
# What cron_fence returns for each manifest is checked in test-addon-manifests.py.
echo "ok: host wiring"

hermes_image_gate "${1:-}" "image checks"

work="$(mktemp -d "${TMPDIR:-/tmp}/addon-cron-fence.XXXXXX")"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/home" "$work/addons-off/gptr"
python3 - "$work/addons-off/gptr/addon.json" <<'PY'
import json
import sys
sys.path.insert(0, "scripts/lib")
import addon_manifest as am
m = json.load(open("addons/gptr/addon.json"))
m["permissions"]["cron"] = False
open(sys.argv[1], "w").write(am.canonical(m))
PY
# Twenty: one workspace in a synthetic store, and a copy of its manifest with cron false.
mkdir -p "$work/addon-data/twenty/instances" "$work/addons-crm-off/twenty"
printf '{"url": "http://twenty-server:3000", "access": "write"}\n' > "$work/addon-data/twenty/instances/main.json"
python3 - "$work/addons-crm-off/twenty/addon.json" <<'PY'
import json
import sys
sys.path.insert(0, "scripts/lib")
import addon_manifest as am
m = json.load(open("addons/twenty/addon.json"))
m["permissions"]["cron"] = False
open(sys.argv[1], "w").write(am.canonical(m))
PY
# Actual (cron "ro", {access} in its instance URL): one connected budget.
mkdir -p "$work/addon-data/actual/instances"
printf '{"server": "local", "sync_id": "s1", "bind": {"default": "rw"}}\n' > "$work/addon-data/actual/instances/main.json"
fn_source "$BOOTSTRAP" fence_mail_from_cron > "$work/fence.sh"
cat > "$work/run.sh" <<'EOF'
set -eu
log() { printf '%s\n' "$*"; }
config_path_for() { echo /h/config.yaml; }
. /w/fence.sh
seed() {
  rm -f /h/.mail-cron-toolsets.json
  printf 'mcp_servers:\n  gptr:\n    url: http://gptr-mcp:8000/sse\n  searxng:\n    url: http://searxng-mcp:8000/mcp\n' > /h/config.yaml
}
seed_budget() {
  rm -f /h/.mail-cron-toolsets.json
  printf 'mcp_servers:\n  budget-main:\n    url: %s\n  searxng:\n    url: http://searxng-mcp:8000/mcp\n' "$1" > /h/config.yaml
}
seed_crm() {
  rm -f /h/.mail-cron-toolsets.json
  printf 'mcp_servers:\n  crm-main:\n    url: http://twenty-server:3000/mcp\n  searxng:\n    url: http://searxng-mcp:8000/mcp\n' > /h/config.yaml
}
cron_list() {
  python3 -c 'import sys; sys.path.insert(0, "/opt/hermes"); import hermes_yaml as y
c = (y.safe_load(open("/h/config.yaml")) or {}).get("platform_toolsets") or {}
print(",".join(map(str, c.get("cron") or [])))'
}
# What Hermes resolves for cron from a list, as the scheduler sees it.
cron_resolves() {
  python3 -c 'import sys; sys.path.insert(0, "/opt/hermes"); import hermes_yaml as y
from hermes_cli.tools_config import _get_platform_tools
print(",".join(sorted(_get_platform_tools(y.safe_load(open("/h/config.yaml")), "cron", include_default_mcp_servers=False))))'
}
seed_list() {
  rm -f /h/.mail-cron-toolsets.json
  printf 'mcp_servers:\n  searxng:\n    url: http://searxng-mcp:8000/mcp\nplatform_toolsets:\n  cron:\n' > /h/config.yaml
  for t in "$@"; do printf '  - %s\n' "$t" >> /h/config.yaml; done
}

ADDON_MANIFEST_PY=/bootstrap/addon_manifest.py
ADDON_DATA_DIR=/w/addon-data
ADDONS_DIR=/bootstrap/addons; seed; fence_mail_from_cron ""
echo "true:$(cron_list)"
echo "resolves:$(cron_resolves)"
# A list an earlier bootstrap pinned (bundle and all) is rewritten.
printf '["hermes-cron", "searxng"]' > /h/.mail-cron-toolsets.json
printf 'mcp_servers:\n  searxng:\n    url: http://searxng-mcp:8000/mcp\nplatform_toolsets:\n  cron:\n  - hermes-cron\n  - searxng\n' > /h/config.yaml
fence_mail_from_cron ""
echo "old-pin:$(cron_list)"
seed_list hermes-cron searxng; fence_mail_from_cron ""
echo "own-bundle:$(cron_list)"
seed_list terminal browser searxng; fence_mail_from_cron ""
echo "own-browser:$(cron_list)"
seed_list terminal web; fence_mail_from_cron ""
echo "own-no-mcp:$(cron_list)"
first="$(cat /h/config.yaml)"; fence_mail_from_cron "" >/dev/null
[ "$(cat /h/config.yaml)" = "$first" ] && echo "own-idempotent"
ADDONS_DIR=/w/addons-off; seed; fence_mail_from_cron ""
echo "false:$(cron_list)"
ADDONS_DIR=/bootstrap/addons; seed_budget http://actual-mcp-ro:3000/main/mcp; fence_mail_from_cron ""
echo "ro-at-ro:$(cron_list)"
seed_budget http://actual-mcp-rw:3000/main/mcp; fence_mail_from_cron ""
echo "ro-at-rw:$(cron_list)"
seed_crm; fence_mail_from_cron ""
echo "crm-ro:$(cron_list)"
ADDONS_DIR=/w/addons-crm-off; seed_crm; fence_mail_from_cron ""
echo "crm-false:$(cron_list)"
ADDONS_DIR=/w/addons-off
ADDON_MANIFEST_PY=/nope.py; fence_mail_from_cron "" >/dev/null
echo "missing:$(cron_list)"
ADDON_MANIFEST_PY=/bootstrap/addon_manifest.py; ADDONS_DIR=/bootstrap/addons; fence_mail_from_cron ""
echo "back:$(cron_list)"
EOF

out="$(docker run --rm --entrypoint /bin/sh \
  -v "$ROOT/addons:/bootstrap/addons:ro" \
  -v "$ROOT/scripts/lib/addon_manifest.py:/bootstrap/addon_manifest.py:ro" \
  -v "$work:/w" -v "$work/home:/h" "$IMAGE" /w/run.sh 2>&1)" || fail "fence run failed: $out"

expect() { printf '%s\n' "$out" | grep -qx -- "$1" || fail "$2 (output: $out)"; }
# Hermes' cron toolsets without browser, as the fence reads them from the image.
core="$(printf '%s\n' "$out" | sed -n 's/^true:\(.*\),gptr,searxng$/\1/p')"
[ -n "$core" ] || fail "cron true pins the core toolsets plus gptr and searxng (output: $out)"
case ",$core," in *,browser,*) fail "the pinned list holds browser: $core" ;; esac
case ",$core," in *,terminal,*) ;; *) fail "the pinned list lost Hermes' other cron toolsets: $core" ;; esac
printf '%s\n' "$out" | grep -q '^resolves:.*browser' && fail "Hermes still resolves browser for cron (output: $out)"
printf '%s\n' "$out" | grep -q '^resolves:.*terminal' || fail "Hermes resolves the pinned list (output: $out)"
expect "false:$core,searxng" "cron false pins a cron list without gptr"
expect "ro-at-ro:$core,budget-main,searxng" "a budget (cron ro) registered at its ro URL stays in cron"
expect "ro-at-rw:$core,searxng" "a budget (cron ro) registered at its rw URL is fenced"
expect "crm-ro:$core,crm-main,searxng" "Twenty (cron true) leaves crm-* entries in cron"
expect "crm-false:$core,searxng" "an instances add-on with cron false fences crm-main"
expect "missing:$core,searxng" "a missing module keeps the list it wrote"
expect "back:$core,crm-main,searxng" "crm-main comes back once the manifest allows cron; browser stays out"
expect "old-pin:$core,searxng" "a list an earlier bootstrap pinned is rewritten without browser"
expect "own-bundle:$core,searxng" "a hand-written hermes-cron is replaced by its toolsets without browser"
expect "own-browser:terminal,searxng" "browser is taken out of a hand-written list"
expect "own-no-mcp:terminal,web,searxng" "a hand-written list naming no MCP server gets the unfenced ones"
expect "own-idempotent" "a second run leaves a hand-written list alone"
echo "ok: image ($IMAGE)"
