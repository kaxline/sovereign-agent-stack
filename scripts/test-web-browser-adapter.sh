#!/bin/sh
# The bootstrap's browser.cdp_url adapter (set_browser_cdp_url): it points the
# attended profiles it manages at the web-browser service and takes that value
# back out when the service is off, leaving any other provider's endpoint alone.
#
#   sh scripts/test-web-browser-adapter.sh [--host-only | --image]
#
# The host part checks the wiring. The image part runs the real function in
# nousresearch/hermes-agent (Hermes's YAML writer lives there) against scratch
# profiles; it is skipped when Docker or the image is missing unless --image
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

# The unattended identity gets no browser: the api bootstrap runs the shared
# steps, so it writes the default profile's value and never its own.
grep -q 'set_browser_cdp_url ""' "$BOOTSTRAP" || fail "the bootstrap writes the default profile's browser.cdp_url"
grep -q 'set_browser_cdp_url "$PROFILE"' "$BOOTSTRAP" || fail "a bootstrap of its own writes its profile's browser.cdp_url"

# Both bootstraps read the agent token, and no other service does: it is the
# agent's client token for the gateway, not the broker's.
for svc in hermes-api-bootstrap hermes-browser-bootstrap; do
  python3 - "$svc" <<'PY' || fail "$svc does not mount the web-browser agent token read-only"
import sys
sys.path.insert(0, "scripts/lib")
from compose_config import render

svc = render("core,web-browser")["services"][sys.argv[1]]
want = "/run/web-browser/agent"
for vol in svc.get("volumes") or []:
    if vol.get("target") == want:
        raise SystemExit(0 if vol.get("read_only") and
                         str(vol.get("source", "")).endswith("web-browser/secrets/agent") else 1)
raise SystemExit(1)
PY
done
echo "ok: host wiring"

hermes_image_gate "${1:-}" "image checks"

work="$(mktemp -d "${TMPDIR:-/tmp}/web-browser-adapter.XXXXXX")"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/hh/profiles/browser"
for fn in is_true replace_file upsert_env remove_env_key config_path_for profile_env_file set_browser_cdp_url; do
  fn_source "$BOOTSTRAP" "$fn" >> "$work/fns.sh"
done
grep -E "^WEB_BROWSER_(CDP_URL|TOKEN_FILE|MARKER)=" "$BOOTSTRAP" > "$work/consts.sh" \
  || fail "the bootstrap defines WEB_BROWSER_CDP_URL and WEB_BROWSER_TOKEN_FILE"
printf 'agent-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n' > "$work/agent-token"

cat > "$work/run.sh" <<'EOF'
set -eu
log() { printf 'log: %s\n' "$*"; }
HERMES_HOME=/h
DEFAULT_ENV=/h/.env
. /w/consts.sh
. /w/fns.sh
WEB_BROWSER_TOKEN_FILE=/w/agent-token
CFG=/h/profiles/browser/config.yaml
ENVF=/h/profiles/browser/.env
MARKER=/h/profiles/browser/.web-browser-cdp-url
BOUNDARY='ws://host.docker.internal:40000/devtools/browser/boundary?token=abc'

cdp_url() {
  python3 -c 'import sys; sys.path.insert(0, "/opt/hermes"); import hermes_yaml as y
d = y.safe_load(open(sys.argv[1])) or {}
b = d.get("browser") or {}
print(b.get("cdp_url") if isinstance(b, dict) and b.get("cdp_url") is not None else "")' "$1"
}
state() {
  printf '%s url=%s token=%s marker=%s\n' "$1" "$(cdp_url "$CFG")" \
    "$(grep -c '^WEB_BROWSER_AGENT_TOKEN=' "$ENVF" 2>/dev/null || true)" \
    "$([ -f "$MARKER" ] && echo yes || echo no)"
}
seed() {
  rm -f "$MARKER" "$ENVF"
  printf 'model:\n  name: test\n' > "$CFG"
  [ -n "${1:-}" ] && printf 'browser:\n  cdp_url: %s\n' "$1" >> "$CFG"
  : > "$ENVF"
}

# Off and nothing written: nothing to do.
seed; WEB_BROWSER_ENABLED=0 set_browser_cdp_url browser >/dev/null; state off-empty
# On: the service's endpoint, the token by reference, the token in the profile's .env.
seed; WEB_BROWSER_ENABLED=1 set_browser_cdp_url browser >/dev/null; state on-fresh
printf 'env: %s\n' "$(cat "$ENVF")"
# A second run changes nothing.
WEB_BROWSER_ENABLED=1 set_browser_cdp_url browser >/dev/null; state on-again
# Another provider's endpoint is left alone, and no token is written for it.
seed "$BOUNDARY"; WEB_BROWSER_ENABLED=1 set_browser_cdp_url browser >/dev/null; state on-foreign
# Off again: a value this stack wrote is taken out, with its token.
seed; WEB_BROWSER_ENABLED=1 set_browser_cdp_url browser >/dev/null
WEB_BROWSER_ENABLED=0 set_browser_cdp_url browser >/dev/null; state off-ours
# Off with another provider's value: left as it is.
seed "$BOUNDARY"; WEB_BROWSER_ENABLED=0 set_browser_cdp_url browser >/dev/null; state off-foreign
# On with no token file: nothing is written, so a half-configured service cannot
# point the agent at an endpoint it cannot authenticate to.
seed; WEB_BROWSER_ENABLED=1 WEB_BROWSER_TOKEN_FILE=/w/missing set_browser_cdp_url browser >/dev/null
state on-no-token
# A profile without a config.yaml is skipped, not created.
mkdir -p /h/profiles/bare
WEB_BROWSER_ENABLED=1 set_browser_cdp_url bare >/dev/null
[ -f /h/profiles/bare/config.yaml ] && echo "bare-created" || echo "bare-untouched"
EOF

out="$(docker run --rm --entrypoint /bin/sh -v "$work:/w" -v "$work/hh:/h" "$IMAGE" /w/run.sh 2>&1)" \
  || fail "the adapter run failed: $out"
said() { printf '%s\n' "$out" | grep -qx -- "$1" || fail "$2 (output: $out)"; }
WANT='ws://web-browser:9222/devtools/browser/stack?token=${WEB_BROWSER_AGENT_TOKEN}'
BOUNDARY='ws://host.docker.internal:40000/devtools/browser/boundary?token=abc'
said "off-empty url= token=0 marker=no" "with the service off there is nothing to write"
said "on-fresh url=$WANT token=1 marker=yes" "the service's endpoint, the token by reference"
said "on-again url=$WANT token=1 marker=yes" "a second run is a no-op"
said "on-foreign url=$BOUNDARY token=0 marker=no" "another provider's endpoint is left alone"
said "off-ours url= token=0 marker=no" "the value this stack wrote is taken out again"
said "off-foreign url=$BOUNDARY token=0 marker=no" "another provider's endpoint survives the service being off"
said "on-no-token url= token=0 marker=no" "no token file means no endpoint"
said "bare-untouched" "a profile without a config.yaml is skipped"
printf '%s\n' "$out" | grep -q '^env: WEB_BROWSER_AGENT_TOKEN=agent-token-' \
  || fail "the token's value goes into the profile's .env (output: $out)"
echo "ok: image ($IMAGE)"
