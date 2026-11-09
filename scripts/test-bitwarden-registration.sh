#!/bin/sh
# Which Hermes profiles get the Bitwarden broker's login backend (bwbroker)
# and its two tools, what the backend lists, and what cron can reach.
#
#   sh scripts/test-bitwarden-registration.sh [--host-only | --image]
#
# The host part checks the wiring in the bootstrap and compose. The image
# part runs the bootstrap's functions, the hermes container's plugin install
# and the plugin itself in nousresearch/hermes-agent, against scratch
# profiles, a synthetic Bitwarden manifest and a fake broker on
# 127.0.0.1:8099. It is skipped when Docker or the image is missing unless
# --image is given.
set -eu

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
BOOTSTRAP=compose/hermes/bootstrap-api-profile.sh
COMPOSE=docker-compose.yml
INSTALL=compose/hermes/14-install-bwbroker-plugin.sh
TAG="${HERMES_AGENT_IMAGE_TAG:-v0.22.7}"
IMAGE="nousresearch/hermes-agent:$TAG"

fail() { echo "FAIL: $*" >&2; exit 1; }

# --- host: wiring -----------------------------------------------------------
grep -q '^register_addon_vault_backends() {' "$BOOTSTRAP" \
  || fail "the bootstrap must define register_addon_vault_backends"
[ "$(grep -c '^ *register_addon_vault_backends "' "$BOOTSTRAP")" = 2 ] \
  || fail "register_addon_vault_backends must run for default and for the bootstrap's own profile"
grep -q 'addon_manifest.cron_fence_toolsets' "$BOOTSTRAP" \
  || fail "the cron fence must use addon_manifest.cron_fence_toolsets"
# shellcheck disable=SC2016 # the literal compose line
[ "$(grep -cF -- '- BITWARDEN_ENABLED=${BITWARDEN_ENABLED:-0}' "$COMPOSE")" = 2 ] \
  || fail "both hermes-*-bootstrap services, and nothing else, must receive BITWARDEN_ENABLED"
hermes_block="$(awk '/^  hermes:$/ {on=1; next} on && /^  [a-z0-9-]+:$/ {exit} on' "$COMPOSE")"
# The flag stays out of hermes' environment, so turning the add-on on or off
# does not make compose recreate hermes.
! printf '%s\n' "$hermes_block" | grep -qF -- '- BITWARDEN_ENABLED=' \
  || fail "hermes must not receive BITWARDEN_ENABLED (flipping it would recreate hermes)"
for mount in \
  './compose/hermes/plugins/bwbroker:/bootstrap/plugins/bwbroker:ro' \
  './compose/hermes/bitwarden_broker_backend.py:/bootstrap/bitwarden_broker_backend.py:ro' \
  "./${INSTALL}:/etc/cont-init.d/14-install-bwbroker-plugin:ro"; do
  printf '%s\n' "$hermes_block" | grep -qF -- "- $mount" || fail "hermes must mount $mount"
done
[ -f compose/hermes/plugins/bwbroker/plugin.yaml ] || fail "missing plugin.yaml"
grep -qx 'name: bwbroker' compose/hermes/plugins/bwbroker/plugin.yaml || fail "plugin.yaml must name bwbroker"
echo "ok: host wiring"

mode="${1:-}"
[ "$mode" = "--host-only" ] && exit 0
if [ "$mode" != "--image" ]; then
  if ! command -v docker >/dev/null 2>&1 || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "skip image checks ($IMAGE not present)"
    exit 0
  fi
fi

# --- image: scratch profiles, synthetic manifest ----------------------------
work="$(mktemp -d "${TMPDIR:-/tmp}/bitwarden-registration.XXXXXX")"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/addons/bitwarden" "$work/home/profiles/browser" "$work/home/profiles/api-server" "$work/base"

python3 - "$work/addons/bitwarden/addon.json" <<'PY'
import json
import sys
sys.path.insert(0, "scripts/lib")
import addon_manifest as am
m = json.load(open("addons/gptr/addon.json"))
m.update(id="io.github.kaxline.stack.bitwarden", title="Bitwarden")
m["builtin"] = {"flag": "BITWARDEN_ENABLED", "profile": "bitwarden", "seed": False}
m["services"] = {"broker": {"compose": "bitwarden-broker", "port": 8099}}
m["uses"] = []
del m["mcp"]
m["permissions"].update(reads=["credentials"], cron=False, egress=["internet"])
m["vaultBackend"] = {"name": "bwbroker", "plugin": "compose/hermes/plugins/bwbroker", "toolset": "bitwarden",
                     "tools": ["bitwarden_request_access", "bitwarden_release_access"],
                     "profiles": {"default": "rw", "browser": "rw"}}
errors = am.validate(m, "builtin")
if errors:
    raise SystemExit(f"fixture manifest is invalid: {errors}")
open(sys.argv[1], "w").write(am.canonical(m))
PY

# Default keeps another plugin, a vault key and a cron list of its own that
# names the bitwarden toolset; none of it may move with the flag off.
cat > "$work/home/config.yaml" <<'EOF'
model:
  default: local
plugins:
  enabled:
  - disk-cleanup
vault:
  onepassword:
    enabled: false
mcp_servers:
  searxng:
    url: http://mcp-searxng:3000/mcp
platform_toolsets:
  cron:
  - hermes-cron
  - bitwarden
  - searxng
EOF
printf 'model:\n  default: local\nmcp_servers:\n  searxng:\n    url: http://mcp-searxng:3000/mcp\n' \
  > "$work/home/profiles/browser/config.yaml"
printf 'model:\n  default: local\n' > "$work/home/profiles/api-server/config.yaml"
cp "$work/home/config.yaml" "$work/base/default.yaml"
cp "$work/home/profiles/browser/config.yaml" "$work/base/browser.yaml"
cp "$work/home/profiles/api-server/config.yaml" "$work/base/api-server.yaml"

for fn in config_path_for addon_vault_plan register_addon_vault_backends fence_mail_from_cron; do
  range='/^'"${fn}"'() {/,/^}/p'
  sed -n "$range" "$BOOTSTRAP" >> "$work/funcs.sh"
done

cat > "$work/run.sh" <<'EOF'
set -eu
log() { printf 'log: %s\n' "$*"; }
HERMES_HOME=/h
ADDONS_DIR=/w/addons
ADDON_MANIFEST_PY=/bootstrap/addon_manifest.py
ADDON_DATA_DIR=/w/addon-data
. /w/funcs.sh
run_all() {
  for p in "" browser api-server; do
    register_addon_vault_backends "$p"
    fence_mail_from_cron "$p"
  done
}
tree() { (cd /h && find . -type f | LC_ALL=C sort | xargs sha256sum); }

export BITWARDEN_ENABLED=0
run_all
cmp -s /h/config.yaml /w/base/default.yaml \
  && cmp -s /h/profiles/browser/config.yaml /w/base/browser.yaml \
  && cmp -s /h/profiles/api-server/config.yaml /w/base/api-server.yaml \
  && echo "off-identical"
[ "$(cd /h && find . -type f | wc -l)" = 3 ] && echo "off-no-new-files"

# A copy in a profile's own plugins dir would shadow the bundled one.
mkdir -p /h/profiles/browser/plugins/bwbroker /h/plugins/disk-cleanup
touch /h/profiles/browser/plugins/bwbroker/plugin.yaml /h/plugins/disk-cleanup/plugin.yaml
BITWARDEN_ENABLED=1
run_all
[ -e /h/profiles/browser/plugins/bwbroker ] || echo "stale-copy-removed"
[ -f /h/plugins/disk-cleanup/plugin.yaml ] && echo "other-plugin-dir-kept"
python3 /w/profile_config.py on
first="$(tree)"
run_all
[ "$(tree)" = "$first" ] && echo "on-idempotent"

# The hermes container's install step, then the plugin as Hermes loads it.
mkdir -p /opt/hermes/plugins/bwbroker && touch /opt/hermes/plugins/bwbroker/stale
BITWARDEN_ENABLED=0 sh /etc/cont-init.d/14-install-bwbroker-plugin
[ -f /opt/hermes/plugins/bwbroker/bitwarden_broker_backend.py ] && echo "bundled-installed-flag-off"
[ -e /opt/hermes/plugins/bwbroker/stale ] || echo "bundled-replaced"
for home in /h /h/profiles/browser /h/profiles/api-server; do
  HERMES_HOME="$home" python3 /w/runtime.py "$home"
done
mkdir -p /u/plugins
cp -R /opt/hermes/plugins/bwbroker /u/plugins/bwbroker
printf 'plugins:\n  enabled:\n  - bwbroker\nvault:\n  bwbroker:\n    enabled: true\n' > /u/config.yaml
HERMES_HOME=/u python3 /w/runtime.py user-copy
HERMES_HOME=/h/profiles/browser python3 /w/backend.py

BITWARDEN_ENABLED=0
run_all
python3 /w/profile_config.py off-again
# The plugin stays installed; with the flag off no profile loads it.
for home in /h /h/profiles/browser; do
  HERMES_HOME="$home" python3 /w/runtime.py "off:$home"
done
# A running process picks the plugin up on a forced rediscovery, as the
# gateway's reload-plugins control verb runs, without a restart.
HERMES_HOME=/h/profiles/browser python3 /w/reload.py
EOF

cat > "$work/profile_config.py" <<'PY'
import sys
sys.path.insert(0, "/opt/hermes")
import hermes_yaml as yaml

phase = sys.argv[1]
paths = {"default": "/h/config.yaml", "browser": "/h/profiles/browser/config.yaml",
         "api-server": "/h/profiles/api-server/config.yaml"}
for profile, path in paths.items():
    cfg = yaml.safe_load(open(path)) or {}
    plugins = (cfg.get("plugins") or {}).get("enabled") or []
    vault = cfg.get("vault") or {}
    known = ((cfg.get("known_plugin_toolsets") or {}).get("cron")) or []
    cron = ((cfg.get("platform_toolsets") or {}).get("cron")) or []
    print(f"{phase}:{profile}:plugins={','.join(plugins)}")
    print(f"{phase}:{profile}:bwbroker={(vault.get('bwbroker') or {}).get('enabled')}")
    print(f"{phase}:{profile}:bitwarden={(vault.get('bitwarden') or {}).get('enabled')}")
    print(f"{phase}:{profile}:onepassword={(vault.get('onepassword') or {}).get('enabled')}")
    print(f"{phase}:{profile}:known_cron={','.join(known)}")
    print(f"{phase}:{profile}:cron={','.join(cron)}")
PY

cat > "$work/runtime.py" <<'PY'
"""Load plugins for one profile home and report what the browser vault and cron see."""
import logging
import sys

logging.disable(logging.WARNING)
sys.path.insert(0, "/opt/hermes")
from hermes_cli.config import load_config
from hermes_cli.plugins import discover_plugins
from hermes_cli.tools_config import _get_platform_tools
from tools.registry import registry
from agent.vault_backends.base import enabled_backends

label = sys.argv[1]
discover_plugins()
backends = sorted(b.name for b in enabled_backends())
tools = []
for name in ("bitwarden_request_access", "bitwarden_release_access"):
    entry = registry.get_entry(name)
    tools.append(f"{name}@{entry.toolset}" if entry else f"{name}@none")
cfg = load_config()
print(f"rt:{label}:backends={','.join(backends)}")
print(f"rt:{label}:tools={','.join(tools)}")
print(f"rt:{label}:cron_has_bitwarden={'bitwarden' in _get_platform_tools(cfg, 'cron')}")
print(f"rt:{label}:cli_has_bitwarden={'bitwarden' in _get_platform_tools(cfg, 'cli')}")
PY

cat > "$work/reload.py" <<'PY'
"""One process: plugins discovered with the add-on off, then turned on and force-rediscovered."""
import logging
import os
import sys

logging.disable(logging.WARNING)
sys.path.insert(0, "/opt/hermes")
import hermes_yaml as yaml
from hermes_cli.plugins import discover_plugins
from tools.registry import registry
from agent.vault_backends.base import enabled_backends


def report(phase):
    backends = ",".join(sorted(b.name for b in enabled_backends()))
    entry = registry.get_entry("bitwarden_request_access")
    print(f"reload:{phase}:backends={backends}")
    print(f"reload:{phase}:tool={entry.toolset if entry else 'none'}")


discover_plugins()
report("before")
path = os.path.join(os.environ["HERMES_HOME"], "config.yaml")
cfg = yaml.safe_load(open(path)) or {}
cfg.setdefault("plugins", {}).setdefault("enabled", []).append("bwbroker")
cfg.setdefault("vault", {})["bwbroker"] = {"enabled": True}
with open(path, "w") as fh:
    yaml.safe_dump(cfg, fh)
discover_plugins(force=True)
report("after")
PY

cat > "$work/backend.py" <<'PY'
"""The bwbroker backend and tools against a fake broker, through the objects Hermes holds."""
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.disable(logging.WARNING)
sys.path.insert(0, "/opt/hermes")
from hermes_cli.plugins import discover_plugins
from tools.registry import registry
from agent.vault_backends.base import enabled_backends
from agent.vault_backend_registry import _registry

failures = []


def check(label, ok, detail=""):
    print(("ok: " if ok else "FAIL: ") + label + ("" if ok else f" ({detail})"))
    if not ok:
        failures.append(label)


ITEMS = [
    {"handle": "bwbroker:work/1111", "label": "Example login", "username": "agent@example.com",
     "origins": ["https://login.example.com", "https://example.com"], "has_totp": True},
    {"handle": "bwbroker:work/2222", "label": "No user", "username": None,
     "origins": ["https://other.example.com"], "has_totp": False},
    {"handle": "bwbroker:work/3333", "label": 7, "username": "x", "origins": [], "has_totp": False},
    {"handle": "vault_9999", "label": "Outside the namespace", "username": "x",
     "origins": ["https://example.com"], "has_totp": False},
]
state = {"mode": "json", "posts": []}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, status, payload, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/items":
            return self._send(404, {"error": "not_found"})
        if state["mode"] == "html":
            return self._send(200, None, b"<html>not json</html>")
        return self._send(200, ITEMS)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        state["posts"].append((self.path, body))
        if self.path == "/requests":
            if body.get("site") == "busy.example.com":
                return self._send(429, {"error": "too_many"})
            if body.get("site") == "odd.example.com":
                return self._send(400, {"error": "Not A Code <script>"})
            return self._send(200, {"request_id": "req-1"})
        if self.path == "/release":
            if body.get("handle") != "bwbroker:work/1111":
                return self._send(404, {"error": "unknown_handle"})
            return self._send(200, {"released": True, "returned": False})
        return self._send(404, {"error": "not_found"})


server = ThreadingHTTPServer(("127.0.0.1", 8099), Fake)
threading.Thread(target=server.serve_forever, daemon=True).start()

discover_plugins()
backend = next(b for b in enabled_backends() if b.name == "bwbroker")
raw = _registry.merged()["bwbroker"]

items = backend.list_items()
check("lists the two well-formed items in its namespace", [m.id for m in items] ==
      ["bwbroker:work/1111", "bwbroker:work/2222"], [m.id for m in items])
first = items[0]
check("metadata fields", (first.kind, first.label, first.origin, first.allowed_origins, first.has_otp,
                          first.identifier, first.identifier_type) ==
      ("login", "Example login", "https://login.example.com",
       ("https://login.example.com", "https://example.com"), True, "agent@example.com", "username"),
      repr(first))
check("no username means no identifier", (items[1].identifier, items[1].identifier_type) == (None, None),
      repr(items[1]))
check("get_meta finds a handle", backend.get_meta("bwbroker:work/1111") == first)
check("get_meta returns None for an unknown handle", backend.get_meta("bwbroker:work/4444") is None)

for method, args in (("resolve_password", ()), ("resolve_otp", ()), ("resolve_secret", ())):
    try:
        getattr(raw, method)("bwbroker:work/1111")
        check(f"{method} raises", False, "returned")
    except RuntimeError as exc:
        check(f"{method} raises the fixed text", str(exc) == "bwbroker never returns values", str(exc))
with raw.fill_context():
    pass
check("fill_context is a no-op context", type(raw.fill_context()).__name__ == "nullcontext")

from cron.execution_identity import enter_cron_execution, exit_cron_execution
token = enter_cron_execution({"id": "job-1", "name": "nightly"}, "exec-1", {"source": "builtin"})
try:
    check("a cron turn lists nothing", backend.list_items() == [])
    check("a cron turn gets no metadata", backend.get_meta("bwbroker:work/1111") is None)
finally:
    exit_cron_execution(token)
check("listing returns after the cron turn", len(backend.list_items()) == 2)

from agent.delegation_context import delegated_child_context
token = enter_cron_execution({"id": "job-2", "name": "nightly"}, "exec-2", {"source": "builtin"})
try:
    with delegated_child_context():
        check("a cron job's delegated child lists nothing", backend.list_items() == [])
        check("a cron job's delegated child gets no metadata", backend.get_meta("bwbroker:work/1111") is None)
finally:
    exit_cron_execution(token)
with delegated_child_context():
    check("a chat's delegated child still lists items", len(backend.list_items()) == 2)

state["mode"] = "html"
check("a non-JSON reply lists nothing", backend.list_items() == [])
check("a non-JSON reply gives no metadata", backend.get_meta("bwbroker:work/1111") is None)
state["mode"] = "json"


def call(tool, args):
    return json.loads(registry.dispatch(tool, args))


got = call("bitwarden_request_access", {"site": "example.com", "reason": "Sign in to read the invoice"})
check("request returns pending with the approve text", got == {
    "pending": True, "request_id": "req-1",
    "approve": "make vault-grant SITE=example.com, or move the login into the agent's folder or collection "
               "in Bitwarden"}, got)
check("request leaves account out when none is given",
      state["posts"][-1] == ("/requests", {"site": "example.com", "reason": "Sign in to read the invoice"}),
      state["posts"][-1])
call("bitwarden_request_access", {"site": "example.com", "reason": "r", "account": "work"})
check("request passes the account", state["posts"][-1][1].get("account") == "work", state["posts"][-1])
got = call("bitwarden_request_access", {"site": "busy.example.com", "reason": "r"})
check("a broker refusal comes back as its code", got == {"success": False, "error": "too_many"}, got)
got = call("bitwarden_request_access", {"site": "odd.example.com", "reason": "r"})
check("a code outside the fixed form is not passed on", got == {"success": False, "error": "unavailable"}, got)
got = call("bitwarden_release_access", {"handle": "bwbroker:work/1111"})
check("release returns released and returned", got == {"released": True, "returned": False}, got)
got = call("bitwarden_release_access", {"handle": "bwbroker:work/4444"})
check("release of an unknown handle", got == {"success": False, "error": "unknown_handle"}, got)
schemas = {name: registry.get_entry(name).schema for name in ("bitwarden_request_access",
                                                               "bitwarden_release_access")}
req = schemas["bitwarden_request_access"]["parameters"]
check("request schema", req["required"] == ["site", "reason"] and req["properties"]["reason"]["maxLength"] == 200
      and "account" in req["properties"], req)
check("release schema", schemas["bitwarden_release_access"]["parameters"]["required"] == ["handle"])
for name, schema in schemas.items():
    text = schema["description"].lower()
    check(f"{name} says a request never grants access and the user approves outside the chat",
          "never grants access" in text and "outside this chat" in text, schema["description"])

server.shutdown()
server.server_close()
check("a broker that is down lists nothing", backend.list_items() == [])
check("a broker that is down gives no metadata", backend.get_meta("bwbroker:work/1111") is None)
got = call("bitwarden_request_access", {"site": "example.com", "reason": "r"})
check("a request with the broker down", got == {"success": False, "error": "broker_unavailable"}, got)
sys.exit(1 if failures else 0)
PY

out="$(docker run --rm --entrypoint /bin/sh \
  -v "$ROOT/scripts/lib/addon_manifest.py:/bootstrap/addon_manifest.py:ro" \
  -v "$ROOT/compose/hermes/plugins/bwbroker:/bootstrap/plugins/bwbroker:ro" \
  -v "$ROOT/compose/hermes/bitwarden_broker_backend.py:/bootstrap/bitwarden_broker_backend.py:ro" \
  -v "$ROOT/$INSTALL:/etc/cont-init.d/14-install-bwbroker-plugin:ro" \
  -v "$work:/w" -v "$work/home:/h" "$IMAGE" /w/run.sh 2>&1)" || fail "image run failed: $out"

expect() { printf '%s\n' "$out" | grep -qxF -- "$1" || fail "$2 (want '$1'; output: $out)"; }
expect 'off-identical' "the flag off leaves every profile config byte-identical"
expect 'off-no-new-files' "the flag off writes no state files"
expect 'stale-copy-removed' "a bwbroker copy in a profile's plugins dir is removed"
expect 'other-plugin-dir-kept' "other plugin dirs are left alone"
expect 'on:default:plugins=disk-cleanup,bwbroker' "default enables bwbroker beside its own plugin"
expect 'on:browser:plugins=bwbroker' "browser enables bwbroker"
expect 'on:api-server:plugins=' "api-server gets no plugin"
for p in default browser; do
  expect "on:$p:bwbroker=True" "$p sets vault.bwbroker.enabled"
  expect "on:$p:known_cron=bitwarden" "$p keeps the bitwarden toolset out of cron"
done
expect 'on:api-server:bwbroker=None' "api-server gets no vault.bwbroker"
expect 'on:api-server:known_cron=' "api-server's cron is left alone"
for p in default browser api-server; do
  expect "on:$p:bitwarden=False" "$p sets vault.bitwarden.enabled false"
done
expect 'on:default:onepassword=False' "other vault keys are kept"
expect 'on:default:cron=hermes-cron,searxng' "the bitwarden toolset is taken out of a hand-written cron list"
expect 'on:browser:cron=' "no cron list is pinned for the plugin alone"
expect 'on-idempotent' "a second run changes nothing"
expect 'bundled-installed-flag-off' "the install step puts the plugin in Hermes's bundled dir with the flag off"
expect 'bundled-replaced' "the install step replaces an earlier copy"
expect "ok: a cron job's delegated child lists nothing" "the delegated-child check ran"
tools_on='bitwarden_request_access@bitwarden,bitwarden_release_access@bitwarden'
for home in /h /h/profiles/browser; do
  expect "rt:$home:backends=bwbroker,local" "$home registers the bwbroker login backend"
  expect "rt:$home:tools=$tools_on" "$home registers both tools under the bitwarden toolset"
  expect "rt:$home:cron_has_bitwarden=False" "$home's cron gets no bitwarden toolset"
  expect "rt:$home:cli_has_bitwarden=True" "$home's chats get the bitwarden toolset"
done
expect 'rt:/h/profiles/api-server:backends=local' "api-server gets no backend"
expect 'rt:/h/profiles/api-server:tools=bitwarden_request_access@none,bitwarden_release_access@none' \
  "api-server gets no tools"
expect 'rt:user-copy:backends=local' "Hermes refuses the backend from a profile's own plugins dir"
if printf '%s\n' "$out" | grep -q '^FAIL'; then
  fail "backend checks: $(printf '%s\n' "$out" | grep '^FAIL')"
fi
expect 'ok: a cron turn lists nothing' "backend checks ran"
expect 'off-again:browser:plugins=' "turning the flag off removes the plugin from browser"
expect 'off-again:default:plugins=disk-cleanup' "turning the flag off keeps default's own plugin"
for p in default browser; do
  expect "off-again:$p:bwbroker=None" "turning the flag off removes vault.bwbroker on $p"
  expect "off-again:$p:known_cron=" "turning the flag off removes the cron entry on $p"
done
expect 'reload:before:backends=local' "a process started with the add-on off has no bwbroker backend"
expect 'reload:after:backends=bwbroker,local' "a forced rediscovery loads the bwbroker backend without a restart"
expect 'reload:after:tool=bitwarden' "a forced rediscovery registers the Bitwarden tools"
for home in /h /h/profiles/browser; do
  expect "rt:off:$home:backends=local" "$home loads no bwbroker backend with the flag off"
  expect "rt:off:$home:tools=bitwarden_request_access@none,bitwarden_release_access@none" \
    "$home registers no Bitwarden tools with the flag off"
done
echo "ok: image ($IMAGE)"
echo "ok"
