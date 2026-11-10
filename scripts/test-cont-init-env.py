#!/usr/bin/env python3
"""Check the cont-init scripts that compose mounts into Hermes containers.

Two checks, for each terminal backend's `docker compose config`:

- Environment: s6-overlay v3 runs /etc/cont-init.d scripts with an empty
  environment. Only a script started through /command/with-contenv sees the
  container's variables; any other script gets its own default for them,
  silently (CR 0021). Each script is checked against the environment of the
  service that mounts it. Variable names are printed, never values.
- Overlay wiring: the scripts skip a patch file that is not there without a
  word (`run_patch`, or `[ -f ... ]` before `python3`), so each patch a script
  runs must be mounted on the same service, as must each helper module the patch names (a module from
  compose/hermes/ or one some service mounts under /bootstrap/). Every
  compose/hermes/patch-*.py must be run by some mounted cont-init script.
  The overlay tests check what each patch does, inside the Hermes image.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import compose_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONT_INIT = "/etc/cont-init.d/"
WITH_CONTENV = "#!/command/with-contenv"
VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
# A patch a script runs: `run_patch /bootstrap/patch-X.py` or `python3 /bootstrap/patch-X.py`.
PATCH_RE = re.compile(r"/bootstrap/patch-[A-Za-z0-9_-]+\.py")
MODULE_RE = re.compile(r"(?:/bootstrap/)?([A-Za-z0-9_]+\.py)")


def load_config(backend: str) -> dict:
    profiles = os.environ.get("COMPOSE_PROFILES", "core")
    return compose_config.render(profiles, {"HERMES_TERMINAL_BACKEND": backend})


def env_names(service: dict) -> set[str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        return {item.split("=", 1)[0] for item in env}
    return set(env)


def variables_read(text: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        names.update(VAR_RE.findall(line))
    return names


def problems(name: str, text: str, service_env: set[str]) -> list[str]:
    """Return one message per script that reads service variables without with-contenv."""
    first = text.splitlines()[0] if text else ""
    if first.startswith(WITH_CONTENV):
        return []
    read = sorted(variables_read(text) & service_env)
    if not read:
        return []
    return [f"{name} reads {', '.join(read)} but starts with {first!r}, not {WITH_CONTENV}"]


def bind_mounts(service: dict) -> dict[str, Path]:
    return {v["target"]: Path(v["source"]) for v in service.get("volumes") or []
            if isinstance(v, dict) and v.get("type") == "bind"}


def helper_modules(config: dict) -> set[str]:
    """File names that are overlay helper modules rather than upstream Hermes files."""
    names = {p.name for p in (ROOT / "compose" / "hermes").glob("*.py") if not p.name.startswith("patch-")}
    for service in (config.get("services") or {}).values():
        for target in bind_mounts(service):
            m = re.fullmatch(r"/bootstrap/([A-Za-z0-9_]+\.py)", target)
            if m and not m.group(1).startswith("patch-"):
                names.add(m.group(1))
    return names


def named_modules(patch_source: str, helpers: set[str]) -> set[str]:
    """Helper modules a patch names in a string literal ("x.py" or "/bootstrap/x.py")."""
    found = set()
    for node in ast.walk(ast.parse(patch_source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            m = MODULE_RE.fullmatch(node.value)
            if m and m.group(1) in helpers:
                found.add(m.group(1))
    return found


def patches_run(script: str) -> list[str]:
    """The /bootstrap/patch-*.py paths a script names outside comments, in order."""
    found: list[str] = []
    for line in script.splitlines():
        if not line.lstrip().startswith("#"):
            found += [p for p in PATCH_RE.findall(line) if p not in found]
    return found


def wiring_problems(name: str, script: str, mounts: dict[str, Path], helpers: set[str]) -> list[str]:
    """One message per patch the script runs, or helper a patch names, that the service does not mount."""
    errors = []
    for patch in patches_run(script):
        if patch not in mounts:
            errors.append(f"{name} runs {patch}, which is not mounted (the script would skip it silently)")
            continue
        for module in sorted(named_modules(mounts[patch].read_text(), helpers)):
            if f"/bootstrap/{module}" not in mounts:
                errors.append(f"{name}: {patch} installs {module}, which is not mounted at /bootstrap/{module}")
    return errors


def self_test() -> list[str]:
    env = {"HERMES_UID", "HERMES_GID"}
    plain = '#!/bin/sh\n# HERMES_UID in a comment is fine\nuid="${HERMES_UID:-1000}"\n'
    errors = []
    if not problems("plain", plain, env):
        errors.append("self-test: a #!/bin/sh script reading HERMES_UID was not flagged")
    if problems("contenv", WITH_CONTENV + " sh\n" + plain.split("\n", 1)[1], env):
        errors.append("self-test: a with-contenv script was flagged")
    if problems("unrelated", '#!/bin/sh\necho "$1 ${dest}"\n', env):
        errors.append("self-test: a script reading only its own variables was flagged")

    with tempfile.TemporaryDirectory() as tmp:
        patch = Path(tmp) / "patch-demo.py"
        patch.write_text('SRC = BOOTSTRAP / "demo_helper.py"\nTARGET = "run_agent.py"\n')
        script = "#!/bin/sh\n# see /bootstrap/patch-commented.py\nrun_patch /bootstrap/patch-demo.py\n"
        helpers = {"demo_helper.py"}
        if not wiring_problems("missing", script, {}, helpers):
            errors.append("self-test: a run_patch of an unmounted patch was not flagged")
        only_patch = {"/bootstrap/patch-demo.py": patch}
        if len(wiring_problems("no helper", script, only_patch, helpers)) != 1:
            errors.append("self-test: an unmounted helper module was not flagged (or an upstream name was)")
        both = {**only_patch, "/bootstrap/demo_helper.py": patch}
        if wiring_problems("wired", script, both, helpers):
            errors.append("self-test: a fully mounted patch was flagged")
    return errors


def check(config: dict, label: str) -> tuple[list[str], int, set[str]]:
    """Errors, the number of cont-init mounts seen, and the patches they run."""
    errors: list[str] = []
    seen = 0
    run: set[str] = set()
    helpers = helper_modules(config)
    for svc_name, service in (config.get("services") or {}).items():
        service_env = env_names(service)
        mounts = bind_mounts(service)
        for target, source in mounts.items():
            if not target.startswith(CONT_INIT):
                continue
            if not source.is_file():
                errors.append(f"{label}: {svc_name} {target}: source {source} is not a file")
                continue
            seen += 1
            name = f"{label}: {svc_name} {target}"
            script = source.read_text()
            errors += problems(name, script, service_env)
            errors += wiring_problems(name, script, mounts, helpers)
            run.update(p.rsplit("/", 1)[1] for p in patches_run(script))
    return errors, seen, run


def main() -> None:
    errors = self_test()
    total = 0
    run: set[str] = set()
    for backend in ("ssh", "local"):
        found, seen, patches = check(load_config(backend), backend)
        errors += found
        total += seen
        run |= patches
    if total == 0:
        errors.append("no /etc/cont-init.d bind mounts found; the check is not looking at the right config")
    for patch in sorted(p.name for p in (ROOT / "compose" / "hermes").glob("patch-*.py")):
        if patch not in run:
            errors.append(f"compose/hermes/{patch} is not run by any mounted cont-init script")
    if errors:
        for err in errors:
            print(f"FAIL {err}", file=sys.stderr)
        raise SystemExit(1)
    print(f"OK cont-init scripts: with-contenv where needed, {len(run)} overlay patches and their modules "
          f"mounted ({total} mounts checked)")


if __name__ == "__main__":
    main()
