#!/command/with-contenv sh
# Keep one gateway process per profile, and keep ended sessions.
#
# Runs before the image's 02-reconcile-profiles (cont-init.d runs in
# lexicographic order, and "019" sorts before "02"), because that script decides
# which gateway slots boot.
#
# Hermes v0.22 runs one gateway per container that multiplexes every named
# profile, and moves each profile's API server onto the default listener at
# /p/<profile>/. This stack depends on per-profile ports (api-server on 8643,
# browser on 8644 for the WebUI, both in docs/extending.md), and on profiles
# that never ran a gateway staying idle. gateway.standalone: true in a profile's
# config.yaml keeps the old topology: the root gateway skips that profile, and
# the profile's own slot boots from its own gateway_state.json as before. With
# every named profile standalone, the root gateway serves only the default
# profile. Upstream calls the key a temporary shim (see docs/hermes.md).
#
# sessions.auto_prune defaults to true and deletes ended sessions inactive for
# retention_days (90) at startup. Pinning it false keeps every session, which
# session_search and the WebUI history rely on.
#
# Both pins are written only where the key is unset, so a value set by hand
# wins. HERMES_PIN_STANDALONE_GATEWAYS=0 or HERMES_PIN_SESSIONS_KEEP=0 skips a pin.
set -u

[ -x /opt/hermes/.venv/bin/python ] || exit 0

s6-setuidgid hermes /opt/hermes/.venv/bin/python - <<'PY' || echo "[019-pin-profile-gateways] warn: exited $?"
import os
import sys
from pathlib import Path

sys.path.insert(0, "/opt/hermes")
from hermes_cli.config import read_user_config_raw  # noqa: E402
from utils import atomic_roundtrip_yaml_update  # noqa: E402

TAG = "[019-pin-profile-gateways]"
home = Path(os.environ.get("HERMES_HOME", "/opt/data"))


def enabled(name: str) -> bool:
    return os.environ.get(name, "1").strip().lower() not in ("0", "false", "no", "off")


def pin(profile: str, cfg_path: Path, key: str, value: bool) -> None:
    section, _, leaf = key.partition(".")
    try:
        cfg = read_user_config_raw(cfg_path)
    except Exception as exc:  # unreadable YAML: leave the file to Hermes' own error path
        print(f"{TAG} warn: cannot read {cfg_path}: {exc}")
        return
    node = cfg.get(section)
    if isinstance(node, dict) and node.get(leaf) is not None:
        return
    atomic_roundtrip_yaml_update(cfg_path, key, value)
    print(f"{TAG} {profile}: set {key}: {str(value).lower()}")


profiles = [("default", home)]
root = home / "profiles"
if root.is_dir():
    profiles += [(p.name, p) for p in sorted(root.iterdir())
                 if p.is_dir() and (p / "SOUL.md").exists() and p.name != "default"]

for name, path in profiles:
    cfg_path = path / "config.yaml"
    if not cfg_path.exists():
        continue
    if name != "default" and enabled("HERMES_PIN_STANDALONE_GATEWAYS"):
        pin(name, cfg_path, "gateway.standalone", True)
    if enabled("HERMES_PIN_SESSIONS_KEEP"):
        pin(name, cfg_path, "sessions.auto_prune", False)
PY
exit 0
