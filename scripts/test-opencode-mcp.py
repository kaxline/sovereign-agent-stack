#!/usr/bin/env python3
"""Checks for opencode-mcp: root handling (needs_access hint, path aliases) and
task bookkeeping (status, wait, continue, eviction of finished tasks).

Stdlib only. fastmcp, httpx and starlette are stubbed so server.py imports
without the container's dependencies; no tool talks to OpenCode here. The
fastmcp stub matches 2.x, where @mcp.tool() returns a tool object rather than
the function, so tests call tools through .fn and a tool calling another tool
directly fails here as it does in the container.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from testkit import Checks  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "compose" / "opencode-mcp" / "server.py"

checks = Checks()


def install_stubs() -> None:
    class FunctionTool:
        def __init__(self, fn):
            self.fn = fn

        def __call__(self, *args, **kwargs):
            raise TypeError(f"'FunctionTool' object is not callable ({self.fn.__name__})")

    class FastMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self, *args, **kwargs):
            return FunctionTool

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


def wait_in_thread(mod, task_id: str, timeout_sec: int, limit: float):
    """Run coding_wait_for_task in a thread; return its result or None if it hangs."""
    box: dict = {}

    def run():
        box["out"] = mod.coding_wait_for_task.fn(task_id, timeout_sec=timeout_sec)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(limit)
    return box.get("out")


def check_tasks(mod) -> None:
    mod.POLL_INTERVAL_SEC = 0.05
    started: list[str] = []
    mod._start_worker = started.append

    def finish(task_id: str, status: str = "completed", error=None) -> None:
        with mod._tasks_lock:
            task = mod._tasks[task_id]
            task.update(status=status, error=error, finished_at=time.time(), progress="done")
            if task["session_id"] is None:
                task["session_id"] = "ses-" + task_id[:4]

    out = mod.coding_start_task.fn("/Users/you/code/app", "  fix it  ")
    first = out["task_id"]
    checks.expect(started == [first], f"start should launch one worker: {started}")
    status = mod.coding_get_task_status.fn(first)
    checks.expect(
        set(status) == {"ok", "task_id", "status", "directory", "session_id"},
        f"status keys while pending: {sorted(status)}",
    )
    checks.expect(mod._tasks[first]["prompt"] == "fix it", "prompt should be stripped")

    # Waiting on a running task times out instead of hanging.
    out = wait_in_thread(mod, first, timeout_sec=1, limit=5)
    checks.expect(out is not None, "wait on a running task did not return")
    checks.expect(bool(out) and out.get("timed_out") is True, f"running wait: {out}")

    # Waiting on a finished task returns its status (this hung on the re-taken lock).
    finish(first)
    out = wait_in_thread(mod, first, timeout_sec=5, limit=3)
    checks.expect(out is not None, "wait on a completed task hung")
    if out:
        checks.expect(out.get("status") == "completed", f"completed wait: {out}")
        checks.expect(out.get("progress") == "done", f"completed wait progress: {out}")
        checks.expect(out == mod.coding_get_task_status.fn(first), "wait and status should agree")
    # A hung wait keeps the lock, and every check below would block on it.
    if not checks.expect(mod._tasks_lock.acquire(timeout=1), "task lock left held after wait"):
        return
    mod._tasks_lock.release()

    out = mod.coding_continue_task.fn(first, "and the docs")
    child = out.get("task_id")
    checks.expect(out.get("parent_task_id") == first, f"continue: {out}")
    checks.expect(child in mod._tasks, "continue should register a task")
    if child in mod._tasks:
        checks.expect(
            set(mod._tasks[child]) == set(mod._tasks[first]) | {"parent_task_id"},
            f"continue task keys: {sorted(mod._tasks[child])}",
        )
        checks.expect(mod._tasks[child]["session_id"] == mod._tasks[first]["session_id"],
                      "continue should reuse the session")
    finish(child, "failed", "boom")
    out = wait_in_thread(mod, child, timeout_sec=5, limit=3)
    checks.expect(bool(out) and out.get("status") == "failed" and out.get("error") == "boom",
                  f"failed wait: {out}")
    out = mod.coding_get_task_status.fn("nope")
    checks.expect(out == {"ok": False, "error": "unknown task_id: nope"}, f"unknown status: {out}")

    # Finished tasks are evicted oldest-first past the cap; running ones stay.
    keep = getattr(mod, "FINISHED_TASKS_KEPT", None)
    checks.expect(isinstance(keep, int) and keep > 0, "FINISHED_TASKS_KEPT should be a positive int")
    if isinstance(keep, int) and keep > 0:
        running = mod.coding_start_task.fn("/Users/you/code/app", "long job")["task_id"]
        for _ in range(keep + 5):
            finish(mod.coding_start_task.fn("/Users/you/code/app", "short job")["task_id"])
        mod.coding_start_task.fn("/Users/you/code/app", "one more")
        finished = [t for t in mod._tasks.values() if t["status"] in ("completed", "failed")]
        checks.expect(len(finished) <= keep, f"{len(finished)} finished tasks kept, cap {keep}")
        checks.expect(running in mod._tasks, "a running task was evicted")
        checks.expect(first not in mod._tasks, "the oldest finished task should be evicted")


def main() -> int:
    install_stubs()
    base = {
        "OPENCODE_WORKSPACE_HOST": "/Users/you/code",
        "CODING_EXTRA_ROOTS": ":/srv/bar:/Users/other/foo",
    }

    # Default (developer stack): hint names the make target.
    dev = load(base)
    out = dev.coding_start_task.fn("/Users/you/elsewhere", "run the tests")
    checks.expect(out.get("needs_access") is True, f"dev: expected needs_access, got {out}")
    hint = out.get("hint", "")
    checks.expect("make coding-root-add DIR=/Users/you/elsewhere" in hint, f"dev hint: {hint}")
    checks.expect("hermes-worker" in hint, f"dev hint should name hermes-worker: {hint}")
    # HERMES_WRITE_SAFE_ROOT is on hermes, so the new root needs it recreated too.
    checks.expect("recreate hermes," in hint, f"dev hint should name hermes: {hint}")
    checks.expect(dev.coding_list_roots.fn()["aliases"] == {}, "dev: aliases should be empty")

    # App-managed: no commands, path named.
    for value in ("1", "true"):
        managed = load({**base, "CODING_ROOTS_APP_MANAGED": value})
        out = managed.coding_start_task.fn("/Users/you/elsewhere", "run the tests")
        hint = out.get("hint", "")
        checks.expect(out.get("needs_access") is True, f"managed={value}: expected needs_access")
        checks.expect("/Users/you/elsewhere" in hint, f"managed={value}: path missing: {hint}")
        for word in ("make", "docker", "compose", "recreate"):
            checks.expect(word not in hint, f"managed={value}: hint mentions {word!r}: {hint}")
    off = load({**base, "CODING_ROOTS_APP_MANAGED": "0"})
    checks.expect("make coding-root-add" in off._needs_access_hint("/x"), "managed=0 should keep dev text")

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
        checks.expect(got == want, f"alias {given!r}: want {want!r}, got {got!r}")
    roots_out = aliased.coding_list_roots.fn()
    checks.expect(
        set(roots_out["aliases"]) == {
            "/opt/projects/foo", "/opt/projects/foo-app",
            "/opt/projects/nested", "/opt/projects/nested/inner",
        },
        f"coding_list_roots aliases: {roots_out['aliases']}",
    )

    # coding_start_task translates before the root check. Stop the worker
    # thread from starting; only the routing is under test.
    aliased._start_worker = lambda task_id: None
    out = aliased.coding_start_task.fn("/opt/projects/foo", "run the tests")
    checks.expect(out.get("ok") is True, f"aliased start: {out}")
    checks.expect(out.get("directory") == "/Users/other/foo", f"aliased start directory: {out}")
    out = aliased.coding_start_task.fn("/opt/projects/foobar", "run the tests")
    checks.expect(out.get("needs_access") is True, f"unaliased /opt path should need access: {out}")

    check_tasks(load(base))

    checks.finish("ok")


if __name__ == "__main__":
    main()
