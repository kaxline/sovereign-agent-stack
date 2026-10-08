#!/usr/bin/env python3
"""Checks for opencode-mcp root handling: needs_access hint text and path aliases.

Stdlib only. fastmcp, httpx and starlette are stubbed so server.py imports
without the container's dependencies; no tool talks to OpenCode here.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "compose" / "opencode-mcp" / "server.py"

failures = 0


def fail(msg: str) -> None:
    global failures
    failures += 1
    print(f"FAIL {msg}", file=sys.stderr)


def check(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def install_stubs() -> None:
    class FastMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self, *args, **kwargs):
            return lambda fn: fn

        def custom_route(self, *args, **kwargs):
            return lambda fn: fn

    stubs = {
        "fastmcp": {"FastMCP": FastMCP},
        "httpx": {},
        "starlette": {},
        "starlette.requests": {"Request": object},
        "starlette.responses": {"JSONResponse": object},
    }
    for name, attrs in stubs.items():
        if name in sys.modules:
            continue
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod


def load(env: dict[str, str]):
    """Import a fresh copy of server.py under env (it reads env at import)."""
    keys = (
        "OPENCODE_WORKSPACE_HOST",
        "CODING_EXTRA_ROOTS",
        "CODING_ROOTS_APP_MANAGED",
        "CODING_PATH_ALIASES",
    )
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("opencode_mcp_server", SERVER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main() -> int:
    install_stubs()
    base = {
        "OPENCODE_WORKSPACE_HOST": "/Users/you/code",
        "CODING_EXTRA_ROOTS": ":/srv/bar:/Users/other/foo",
    }

    # Default (developer stack): hint names the make target.
    dev = load(base)
    out = dev.coding_start_task("/Users/you/elsewhere", "run the tests")
    check(out.get("needs_access") is True, f"dev: expected needs_access, got {out}")
    hint = out.get("hint", "")
    check("make coding-root-add DIR=/Users/you/elsewhere" in hint, f"dev hint: {hint}")
    check("hermes-worker" in hint, f"dev hint should name hermes-worker: {hint}")
    check(dev.coding_list_roots()["aliases"] == {}, "dev: aliases should be empty")

    # App-managed: no commands, path named.
    for value in ("1", "true"):
        managed = load({**base, "CODING_ROOTS_APP_MANAGED": value})
        out = managed.coding_start_task("/Users/you/elsewhere", "run the tests")
        hint = out.get("hint", "")
        check(out.get("needs_access") is True, f"managed={value}: expected needs_access")
        check("/Users/you/elsewhere" in hint, f"managed={value}: path missing: {hint}")
        for word in ("make", "docker", "compose", "recreate"):
            check(word not in hint, f"managed={value}: hint mentions {word!r}: {hint}")
    off = load({**base, "CODING_ROOTS_APP_MANAGED": "0"})
    check("make coding-root-add" in off._needs_access_hint("/x"), "managed=0 should keep dev text")

    # Aliases: two projects share a folder name (foo) under different roots.
    aliased = load({
        **base,
        "CODING_PATH_ALIASES": (
            "/opt/projects/foo=/Users/other/foo; /opt/projects/foo-app/=/Users/you/code/foo;"
            "/opt/projects/nested=/srv/bar;/opt/projects/nested/inner=/Users/you/code/inner;"
            "bad-entry;/opt/projects/rel=relative"
        ),
    })
    cases = {
        "/opt/projects/foo": "/Users/other/foo",
        "/opt/projects/foo/": "/Users/other/foo",
        "/opt/projects/foo/src": "/Users/other/foo/src",
        "/opt/projects/foo-app": "/Users/you/code/foo",
        "/opt/projects/foobar": "/opt/projects/foobar",
        "/opt/projects/nested/x": "/srv/bar/x",
        "/opt/projects/nested/inner/y": "/Users/you/code/inner/y",
        "/opt/projects/rel": "/opt/projects/rel",
        "/Users/you/code/app": "/Users/you/code/app",
    }
    for given, want in cases.items():
        got = aliased._resolve_alias(given)
        check(got == want, f"alias {given!r}: want {want!r}, got {got!r}")
    roots_out = aliased.coding_list_roots()
    check(
        set(roots_out["aliases"]) == {
            "/opt/projects/foo", "/opt/projects/foo-app",
            "/opt/projects/nested", "/opt/projects/nested/inner",
        },
        f"coding_list_roots aliases: {roots_out['aliases']}",
    )

    # coding_start_task translates before the root check. Stop the worker
    # thread from starting; only the routing is under test.
    aliased._start_worker = lambda task_id: None
    out = aliased.coding_start_task("/opt/projects/foo", "run the tests")
    check(out.get("ok") is True, f"aliased start: {out}")
    check(out.get("directory") == "/Users/other/foo", f"aliased start directory: {out}")
    out = aliased.coding_start_task("/opt/projects/foobar", "run the tests")
    check(out.get("needs_access") is True, f"unaliased /opt path should need access: {out}")

    if failures:
        print(f"{failures} check(s) failed", file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
