#!/usr/bin/env python3
"""coding-root.sh add/remove --apply [--force] (CR 0046 R3).

Runs in a scratch checkout with STACK_PYTHON=host and a fake `docker` on PATH
that logs its arguments and answers `compose ps`, the two `compose exec`
probes (opencode-mcp's GET /tasks, hermes' session.live_turns) and
`compose up` from environment variables. Covers the idle, busy, unreadable
and --force cases for the synced and plain forms, the no-op cases, a failed
recreate, and that busy changes nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from testkit import Checks  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    "scripts/coding-root.sh",
    "scripts/lib/env.sh",
    "scripts/lib/python.sh",
    "scripts/lib/roots.sh",
    "scripts/lib/override_mounts.py",
    "scripts/lib/json-lines.sh",
    "scripts/lib/data-root.sh",
    "scripts/lib/stack-lock.sh",
    "scripts/lib/coding-roots.sh",
    "scripts/lib/coding_roots.py",
    "scripts/lib/live_turns.py",
    "docker-compose.override.yml.example",
)
UUID = "3f2b8c1e-7d4a-4e9b-9c2f-1a2b3c4d5e6f"
CPATH = "/Users/you/code/app"
RECREATE = ["hermes-worker", "hermes", "opencode", "opencode-mcp"]
TASK = {"task_id": "t1", "status": "running", "directory": CPATH, "started_at": "2026-11-23T10:00:00Z"}
TURN = {"session_id": "s1", "status": "working", "turn_started_at": 1795000000.0, "idle_s": 1.5}

FAKE_DOCKER = r"""#!/usr/bin/env bash
args="$*"
printf '%s\t%s\n' "$PWD" "${args//$'\n'/ }" >> "$FAKE_DOCKER_LOG"
case "$*" in
  "compose ps --status running -q opencode-mcp") [ "${FAKE_MCP_UP:-1}" = 1 ] && echo c1 ;;
  "compose ps --status running -q hermes") [ "${FAKE_HERMES_UP:-1}" = 1 ] && echo c2 ;;
  "compose exec -T opencode-mcp "*)
    [ "$FAKE_TASKS" = fail ] && exit 1
    printf '%s\n' "$FAKE_TASKS" ;;
  "compose exec -T hermes "*)
    cat > "$FAKE_DOCKER_LOG.stdin"
    [ "$FAKE_TURNS" = fail ] && exit 1
    printf '%s\n' "$FAKE_TURNS" ;;
  "compose up "*) echo "compose up output"; exit "${FAKE_UP_RC:-0}" ;;
  *) echo "unexpected docker call: $*" >&2; exit 99 ;;
esac
"""

checks = Checks()


def checkout(base: Path, env: str) -> Path:
    root = Path(tempfile.mkdtemp(dir=base))
    for rel in FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, root / rel)
    (root / ".env").write_text(env)
    shutil.copy(ROOT / "docker-compose.override.yml.example", root / "docker-compose.override.yml")
    return root


class Stack:
    def __init__(self, base: Path, env: str) -> None:
        self.root = checkout(base, env)
        self.bin = self.root / "fakebin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        self.log = self.root / "docker.log"

    def run(self, *args: str, tasks: object = (), turns: object = (), **extra: str):
        """coding-root.sh ARGS from a different cwd, so compose must cd itself."""
        self.log.write_text("")
        env = os.environ.copy()
        env.pop("ASSISTANT_DATA_ROOT", None)
        env.update({
            "STACK_PYTHON": "host",
            "PATH": f"{self.bin}:{env['PATH']}",
            "FAKE_DOCKER_LOG": str(self.log),
            "FAKE_TASKS": tasks if isinstance(tasks, str) else json.dumps(list(tasks), separators=(",", ":")),
            "FAKE_TURNS": turns if isinstance(turns, str) else json.dumps(list(turns), separators=(",", ":")),
            **extra,
        })
        return subprocess.run(["bash", str(self.root / "scripts/coding-root.sh"), *args],
                              cwd="/", env=env, capture_output=True, text=True)

    def calls(self) -> list[str]:
        rows = [line.split("\t", 1) for line in self.log.read_text().splitlines() if line]
        for cwd, _ in rows:
            checks.expect(cwd == str(self.root), f"docker ran in {cwd}, not the stack folder")
        return [args for _, args in rows]

    def ups(self) -> list[str]:
        return [c for c in self.calls() if c.startswith("compose up ")]

    def state(self) -> tuple[str, ...]:
        reg = self.root / "data/coding-roots/roots.json"
        return ((self.root / ".env").read_text(), (self.root / "docker-compose.override.yml").read_text(),
                reg.read_text() if reg.exists() else "")


def one_json(proc: subprocess.CompletedProcess[str]) -> dict:
    lines = proc.stdout.splitlines()
    ok = len(lines) == 1 and lines[0].startswith("{")
    checks.expect(ok, f"stdout is not one JSON line: {proc.stdout!r} {proc.stderr!r}")
    return json.loads(lines[0]) if ok else {}


UP = "compose up -d --wait --force-recreate " + " ".join(RECREATE)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        ws = base / "ws"
        plain = base / "plain"
        for d in (ws, plain, plain / "sub"):
            d.mkdir()
        env = (
            f"HERMES_TERMINAL_BACKEND=ssh\nOPENCODE_WORKSPACE_HOST={ws}\nCODING_EXTRA_ROOTS=\n"
            f"HERMES_UID={os.getuid()}\nHERMES_GID={os.getgid()}\n"
        )
        s = Stack(base, env)
        add = ("add", "--container-path", CPATH, "--id", UUID, "--json")
        remove = ("remove", "--id", UUID, "--json")

        # Without --apply nothing touches docker and the done line is as before.
        proc = s.run(*add)
        out = one_json(proc)
        checks.expect(proc.returncode == 0 and "recreated" not in out and out.get("recreate") == RECREATE,
                      f"add without --apply: {out}")
        checks.expect(s.calls() == [], f"add without --apply called docker: {s.calls()}")
        s.run(*remove, "--delete-folder")

        # Busy: a running task, a live turn, or both. Nothing is written.
        before = s.state()
        for tasks, turns, words in (([TASK], [], ["coding task"]), ([], [TURN], ["chat turn"]),
                                    ([TASK], [TURN], ["coding task", "chat turn"])):
            proc = s.run(*add, "--apply", tasks=tasks, turns=turns)
            out = one_json(proc)
            checks.expect(proc.returncode == 1 and out.get("error") == "busy", f"busy add: rc={proc.returncode} {out}")
            checks.expect(out.get("tasks") == tasks and out.get("liveTurns") == turns
                      and out.get("liveTurnsReason") is None, f"busy lists: {out}")
            for word in words:
                checks.expect(word in out.get("message", ""), f"busy message lacks {word!r}: {out}")
            checks.expect("Nothing was changed" in out.get("message", ""), f"busy message: {out}")
            checks.expect(s.ups() == [], f"busy add recreated: {s.calls()}")
            checks.expect(s.state() == before, "busy add changed a file")
            checks.expect(not (s.root / "data/coding-roots" / UUID).exists(), "busy add made the folder")

        # Unreadable tasks count as busy; a stopped opencode-mcp counts as idle.
        for kw, label in (({"tasks": "fail"}, "tasks probe failed"), ({"tasks": "not json"}, "tasks probe garbled")):
            proc = s.run(*add, "--apply", **kw)
            out = one_json(proc)
            checks.expect(proc.returncode == 1 and out.get("error") == "busy" and "cannot read" in out.get("message", ""),
                          f"{label}: {out}")
            checks.expect(s.state() == before, f"{label}: changed a file")
        proc = s.run(*add, "--apply", tasks="fail", turns="fail", FAKE_MCP_UP="0", FAKE_HERMES_UP="0")
        out = one_json(proc)
        checks.expect(proc.returncode == 0 and out.get("interrupted") == {"tasks": [], "liveTurns": None}
                      and out.get("liveTurnsReason") == "hermes is not running",
                      f"stopped services: {proc.stdout}{proc.stderr}")
        checks.expect(not any(c.startswith("compose exec") for c in s.calls()), "probed a stopped service")
        s.run(*remove)
        # Unknown live turns (a gateway without session.live_turns) don't block.
        proc = s.run(*add, "--apply", turns="fail")
        out = one_json(proc)
        checks.expect(proc.returncode == 0 and out.get("recreated") == RECREATE
                      and out.get("interrupted") == {"tasks": [], "liveTurns": None}
                      and out.get("liveTurnsReason") == "hermes did not answer session.live_turns",
                      f"unknown turns: {proc.stdout}{proc.stderr}")
        proc = s.run(*remove, "--apply", turns="not json")
        checks.expect(proc.returncode == 0 and one_json(proc).get("liveTurnsReason") is not None,
                      f"garbled turns: {proc.stdout}")

        # Idle: the change lands and the four services are recreated.
        proc = s.run(*add, "--apply")
        out = one_json(proc)
        host = s.root / "data/coding-roots" / UUID
        checks.expect(
            out == {"phase": "done", "id": UUID, "hostPath": str(host), "containerPath": CPATH, "created": True,
                    "recreate": RECREATE, "recreated": RECREATE, "interrupted": {"tasks": [], "liveTurns": []}, "liveTurnsReason": None},
            f"idle add: {out} {proc.stderr}",
        )
        checks.expect(s.ups() == [UP], f"idle add compose calls: {s.calls()}")
        checks.expect("session.live_turns" in Path(f"{s.log}.stdin").read_text(),
                      "hermes probe was not fed live_turns.py")
        checks.expect(host.is_dir() and CPATH in (s.root / ".env").read_text(), "idle add did not write")

        # A repeated add still applies (an earlier add may have skipped --apply).
        proc = s.run(*add, "--apply")
        out = one_json(proc)
        checks.expect(out.get("created") is False and out.get("recreated") == RECREATE, f"repeat add: {out}")

        # Busy remove changes nothing; --force removes and names what it stopped.
        before = s.state()
        proc = s.run(*remove, "--apply", tasks=[TASK])
        checks.expect(proc.returncode == 1 and one_json(proc).get("error") == "busy", f"busy remove: {proc.stdout}")
        checks.expect(s.state() == before, "busy remove changed a file")
        proc = s.run(*remove, "--apply", "--force", tasks=[TASK], turns=[TURN])
        out = one_json(proc)
        checks.expect(proc.returncode == 0 and out.get("removed") is True, f"forced remove: {out} {proc.stderr}")
        checks.expect(out.get("recreated") == RECREATE and out.get("interrupted") == {"tasks": [TASK], "liveTurns": [TURN]},
                      f"forced remove report: {out}")
        checks.expect(s.ups() == [UP], f"forced remove compose calls: {s.calls()}")

        # --force with unreadable answers goes ahead and reports null.
        proc = s.run(*add, "--apply", "--force", tasks="fail", turns="fail")
        out = one_json(proc)
        checks.expect(proc.returncode == 0 and out.get("interrupted") == {"tasks": None, "liveTurns": None},
                      f"forced add, unreadable: {out}")
        s.run(*remove)

        # No-ops: an unknown ID, a covered plain path. No probe, no recreate.
        proc = s.run("remove", "--id", "never", "--json", "--apply", tasks=[TASK])
        out = one_json(proc)
        checks.expect(proc.returncode == 0 and out.get("removed") is False and out.get("recreated") == []
                      and out.get("interrupted") == {"tasks": [], "liveTurns": []}, f"no-op remove: {out}")
        checks.expect(s.calls() == [], f"no-op remove called docker: {s.calls()}")

        # Plain forms: busy, idle add, covered add, idle remove, absent remove.
        proc = s.run("add", str(plain), "--json", "--apply", turns=[TURN])
        checks.expect(proc.returncode == 1 and one_json(proc).get("error") == "busy", f"busy plain add: {proc.stdout}")
        checks.expect(str(plain) not in (s.root / ".env").read_text(), "busy plain add wrote .env")
        proc = s.run("add", str(plain), "--json", "--apply")
        out = one_json(proc)
        checks.expect(out == {"phase": "done", "path": str(plain), "changed": True, "recreate": RECREATE,
                              "recreated": RECREATE, "interrupted": {"tasks": [], "liveTurns": []}, "liveTurnsReason": None}, f"plain add: {out}")
        proc = s.run("add", str(plain / "sub"), "--json", "--apply", tasks=[TASK])
        out = one_json(proc)
        checks.expect(proc.returncode == 0 and out.get("changed") is False and out.get("recreated") == [],
                      f"covered plain add: {out}")
        proc = s.run("remove", str(plain), "--json", "--apply")
        out = one_json(proc)
        checks.expect(out.get("changed") is True and out.get("recreated") == RECREATE, f"plain remove: {out}")
        checks.expect(str(plain) not in (s.root / ".env").read_text(), "plain remove left .env")
        proc = s.run("remove", str(plain), "--json", "--apply")
        out = one_json(proc)
        checks.expect(out.get("changed") is False and out.get("recreated") == [], f"absent plain remove: {out}")

        # A failed recreate: the change stays written, compose_failed with the log tail.
        proc = s.run(*add, "--apply", FAKE_UP_RC="1")
        out = one_json(proc)
        checks.expect(proc.returncode == 1 and out.get("error") == "compose_failed"
                      and "compose up output" in out.get("logTail", []), f"failed up: {out}")
        checks.expect(CPATH in (s.root / ".env").read_text(), "failed up rolled the change back")
        s.run(*remove)

        # Text mode names what it recreated; --force alone and --apply on list are bad input.
        proc = s.run("add", "--container-path", CPATH, "--id", UUID, "--apply")
        checks.expect(proc.returncode == 0 and "Recreated hermes-worker hermes opencode opencode-mcp" in proc.stdout,
                      f"text apply: {proc.stdout}{proc.stderr}")
        for args in (("remove", "--id", UUID, "--force", "--json"), ("list", "--apply", "--json")):
            proc = s.run(*args)
            checks.expect(proc.returncode == 2 and one_json(proc).get("error") == "bad_input", f"{args}: {proc.stdout}")

        # Local backend: hermes is the terminal service, listed once.
        s2 = Stack(base, env.replace("=ssh", "=local"))
        proc = s2.run(*add, "--apply")
        out = one_json(proc)
        checks.expect(out.get("recreated") == ["hermes", "opencode", "opencode-mcp"], f"local backend: {out}")

    checks.finish("OK coding-root --apply")


if __name__ == "__main__":
    main()
