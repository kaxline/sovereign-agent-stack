#!/command/with-contenv sh
# Set up the gateway topology, and keep ended sessions.
#
# Runs before the image's 02-reconcile-profiles (cont-init.d runs in
# lexicographic order, and "019" sorts before "02"), because that script decides
# which gateway slots boot.
#
# Hermes v0.22 runs one gateway per container that multiplexes every named
# profile, and moves each profile's API server onto the default listener at
# /p/<profile>/. HERMES_GATEWAY_TOPOLOGY picks the layout:
#
# standalone (default): one gateway process per profile, with api-server on
#   8643 and browser on 8644 (docs/extending.md). gateway.standalone: true in a
#   profile's config.yaml keeps that layout: the root gateway skips the profile,
#   and the profile's own slot boots from its own gateway_state.json as before.
#   Upstream calls the key a temporary shim (see docs/hermes.md).
#
# multiplex: one gateway serves every profile. The default profile needs an
#   API_SERVER_KEY of its own for the listener to exist, so one is generated
#   into $HERMES_HOME/.env if missing. Named profiles that never ran a gateway
#   are parked (gateway.parked, as `hermes -p X gateway stop` writes) once, and
#   recorded in gateway_topology.json so a profile unparked later stays
#   unparked. gateway-compat-proxy keeps 8643 and 8644 answering.
#
# Unset, HERMES_PIN_STANDALONE_GATEWAYS=0 (the older name) means multiplex.
#
# sessions.auto_prune defaults to true and deletes ended sessions inactive for
# retention_days (90) at startup. Pinning it false keeps every session, which
# session_search and the WebUI history rely on.
#
# Both pins are written only where the key is unset, so a value set by hand
# wins. HERMES_PIN_SESSIONS_KEEP=0 skips the sessions pin.
set -u

[ -x /opt/hermes/.venv/bin/python ] || exit 0

s6-setuidgid hermes /opt/hermes/.venv/bin/python - <<'PY' || echo "[019-pin-profile-gateways] warn: exited $?"
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, "/opt/hermes")
from hermes_cli.config import read_user_config_raw  # noqa: E402
from utils import atomic_roundtrip_yaml_update  # noqa: E402

TAG = "[019-pin-profile-gateways]"
home = Path(os.environ.get("HERMES_HOME", "/opt/data"))


def enabled(name: str) -> bool:
    return os.environ.get(name, "1").strip().lower() not in ("0", "false", "no", "off")


def topology() -> str:
    value = os.environ.get("HERMES_GATEWAY_TOPOLOGY", "").strip().lower()
    if value in ("standalone", "multiplex"):
        return value
    if value:
        print(f"{TAG} warn: HERMES_GATEWAY_TOPOLOGY={value!r} is not standalone or multiplex; using standalone")
        return "standalone"
    return "standalone" if enabled("HERMES_PIN_STANDALONE_GATEWAYS") else "multiplex"


def read_cfg(cfg_path: Path):
    try:
        return read_user_config_raw(cfg_path)
    except Exception as exc:  # unreadable YAML: leave the file to Hermes' own error path
        print(f"{TAG} warn: cannot read {cfg_path}: {exc}")
        return None


def pin(profile: str, cfg_path: Path, key: str, value: bool) -> None:
    section, _, leaf = key.partition(".")
    cfg = read_cfg(cfg_path)
    if cfg is None:
        return
    node = cfg.get(section)
    if isinstance(node, dict) and node.get(leaf) is not None:
        return
    atomic_roundtrip_yaml_update(cfg_path, key, value)
    print(f"{TAG} {profile}: set {key}: {str(value).lower()}")


def desired_state(path: Path):
    try:
        data = json.loads((path / "gateway_state.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data.get("desired_state") or data.get("gateway_state")


def ensure_default_key() -> None:
    env_path = home / ".env"
    text = env_path.read_text() if env_path.exists() else ""
    if any(line.split("=", 1)[0].strip() == "API_SERVER_KEY" and line.split("=", 1)[1].strip()
           for line in text.splitlines() if "=" in line and not line.lstrip().startswith("#")):
        return
    sep = "" if not text or text.endswith("\n") else "\n"
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(f"{sep}API_SERVER_KEY={secrets.token_urlsafe(32)}\n")
    print(f"{TAG} default: generated API_SERVER_KEY in .env")


def park_idle(named: list) -> None:
    record_path = home / "gateway_topology.json"
    try:
        record = json.loads(record_path.read_text())
    except (OSError, ValueError):
        record = {}
    seen = set(record.get("parked_once", []))
    changed = False
    for name, path in named:
        if name in seen:
            continue
        seen.add(name)
        changed = True
        if (path / "gateway.parked").exists() or desired_state(path) == "running":
            continue
        (path / "gateway.parked").touch()
        print(f"{TAG} {name}: parked (never ran a gateway; `hermes -p {name} gateway start` serves it)")
    if changed:
        record["parked_once"] = sorted(seen)
        tmp = record_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2) + "\n")
        os.replace(tmp, record_path)


mode = topology()
print(f"{TAG} topology: {mode}")
named = []
root = home / "profiles"
if root.is_dir():
    named = [(p.name, p) for p in sorted(root.iterdir())
             if p.is_dir() and (p / "SOUL.md").exists() and p.name != "default"]

for name, path in [("default", home)] + named:
    cfg_path = path / "config.yaml"
    if not cfg_path.exists():
        continue
    if name != "default" and mode == "standalone":
        pin(name, cfg_path, "gateway.standalone", True)
    if name != "default" and mode == "multiplex":
        cfg = read_cfg(cfg_path) or {}
        gw = cfg.get("gateway")
        if isinstance(gw, dict) and gw.get("standalone") is True:
            print(f"{TAG} warn: {name} keeps gateway.standalone: true and runs its own gateway; "
                  "./scripts/gateway-topology.sh multiplex clears it")
    if enabled("HERMES_PIN_SESSIONS_KEEP"):
        pin(name, cfg_path, "sessions.auto_prune", False)

if mode == "multiplex":
    ensure_default_key()
    park_idle(named)
PY
exit 0
