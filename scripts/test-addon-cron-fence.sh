#!/bin/sh
# The bootstrap's cron fence (fence_mail_from_cron) keeps add-ons whose
# manifest doesn't set permissions.cron true out of platform_toolsets.cron.
#
#   sh scripts/test-addon-cron-fence.sh [--host-only | --image]
#
# The host part checks the wiring. The image part runs the real function in
# nousresearch/hermes-agent (Hermes's YAML writer lives there) against scratch
# configs; it is skipped when Docker or the image is missing unless --image
# is given.
set -eu

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
BOOTSTRAP=compose/hermes/bootstrap-api-profile.sh
TAG="${HERMES_AGENT_IMAGE_TAG:-v0.22.7}"
IMAGE="nousresearch/hermes-agent:$TAG"

fail() { echo "FAIL: $*" >&2; exit 1; }

# shellcheck disable=SC2016 # the literal bootstrap line
grep -q '_out="$(python3 - "$_fmc_cfg" "$ADDON_MANIFEST_PY" "$ADDONS_DIR" <<' "$BOOTSTRAP" \
  || fail "fence_mail_from_cron must pass the add-on manifests to its python"
grep -q 'addon_manifest.cron_fence' "$BOOTSTRAP" || fail "the fence must use addon_manifest.cron_fence"
echo "ok: host wiring"

mode="${1:-}"
[ "$mode" = "--host-only" ] && exit 0
if [ "$mode" != "--image" ]; then
  if ! command -v docker >/dev/null 2>&1 || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "skip image checks ($IMAGE not present)"
    exit 0
  fi
fi

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
sed -n '/^fence_mail_from_cron() {/,/^}/p' "$BOOTSTRAP" > "$work/fence.sh"
cat > "$work/run.sh" <<'EOF'
set -eu
log() { printf '%s\n' "$*"; }
config_path_for() { echo /h/config.yaml; }
. /w/fence.sh
seed() {
  rm -f /h/.mail-cron-toolsets.json
  printf 'mcp_servers:\n  gptr:\n    url: http://gptr-mcp:8000/sse\n  searxng:\n    url: http://searxng-mcp:8000/mcp\n' > /h/config.yaml
}
cron_list() { tr -d '\n ' < /h/config.yaml | sed -n 's/.*cron:\(.*\)/\1/p'; }

ADDON_MANIFEST_PY=/bootstrap/addon_manifest.py
ADDONS_DIR=/bootstrap/addons; seed; fence_mail_from_cron ""
echo "true:$(cron_list)"
ADDONS_DIR=/w/addons-off; seed; fence_mail_from_cron ""
echo "false:$(cron_list)"
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
expect 'true:' "cron true leaves platform_toolsets.cron unset"
expect 'false:-hermes-cron-searxng' "cron false pins a cron list without gptr"
expect 'missing:-hermes-cron-searxng' "a missing module keeps the list it wrote"
expect 'back:' "the list is removed once the manifest allows cron"
echo "ok: image ($IMAGE)"
