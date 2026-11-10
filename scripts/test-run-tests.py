#!/usr/bin/env python3
"""Checks scripts/run-tests.py against a throwaway tree of fake tests.

Covers the manifest checks (unlisted, missing, duplicate, undeclared tags,
the image tag against the image gate), selection, how docker, macos and image
tests are run or skipped, exit 77 as a skip, --strict, the per-test timeout
(which also stops the test's children), parallel runs and the times file.
Needs no Docker: the environment is passed in.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
from testkit import Checks  # noqa: E402

spec = importlib.util.spec_from_file_location("run_tests", ROOT / "scripts" / "run-tests.py")
rt = importlib.util.module_from_spec(spec)
sys.modules["run_tests"] = rt  # dataclasses look their module up here
spec.loader.exec_module(rt)

checks = Checks()
IMAGE = "nousresearch/hermes-agent:test"
FULL = rt.Env(docker=True, image=IMAGE, image_present=True, macos=True)
BARE = rt.Env(docker=False, image=IMAGE, image_present=False, macos=False)
NO_IMAGE = rt.Env(docker=True, image=IMAGE, image_present=False, macos=True)

HEADER = """\
@tag docker  needs Docker
@tag image   has an image part
@tag macos   needs macOS
@tag slow    a group
"""

# Each fake test records its argv and HERMES_IMAGE_REQUIRED in out/<name>.
TESTS = {
    "scripts/test-pass.sh": "#!/bin/sh\necho ran \"$@\" \"${HERMES_IMAGE_REQUIRED:-}\" > \"$OUT/pass\"\necho ok pass\n",
    "scripts/test-fail.py": "import sys\nprint('line one')\nprint('FAIL broken thing', file=sys.stderr)\n"
                            "raise SystemExit(3)\n",
    "scripts/test-skip.sh": "#!/usr/bin/env bash\necho 'SKIP no widget here'\nexit 77\n",
    "scripts/test-docker.sh": "#!/bin/sh\ntouch \"$OUT/docker\"\n",
    "scripts/test-mac.py": "open(__import__('os').environ['OUT'] + '/mac', 'w').close()\n",
    # Split so this file does not itself look like an image test to the manifest check.
    "scripts/test-image.py": "import os, sys\n# hermes_image." "maybe_run(DRIVER)\n"
                             "open(os.environ['OUT'] + '/image', 'w').write(' '.join(sys.argv[1:]) + '|' "
                             "+ os.environ.get('HERMES_IMAGE_REQUIRED', ''))\n",
    "imessage/tests/test-slow-a.sh": "#!/bin/sh\nsleep 1\n",
    "imessage/tests/test-slow-b.sh": "#!/bin/sh\nsleep 1\n",
}
MANIFEST = HEADER + """\
scripts/test-pass.sh
scripts/test-fail.py
scripts/test-skip.sh
scripts/test-docker.sh          docker
scripts/test-mac.py             macos
scripts/test-image.py           image
imessage/tests/test-slow-a.sh   slow
imessage/tests/test-slow-b.sh   slow
"""


def tree(tmp: Path, manifest: str = MANIFEST, tests: dict | None = None) -> Path:
    root = tmp / "root"
    for rel, body in (tests or TESTS).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
    (root / "scripts" / "tests.manifest").write_text(manifest)
    (root / "out").mkdir(exist_ok=True)
    return root


def run(root: Path, *args: str, env: rt.Env = FULL) -> tuple[int, str]:
    out = io.StringIO()
    os.environ["OUT"] = str(root / "out")
    rc = rt.main(["--root", str(root), "--manifest", str(root / "scripts" / "tests.manifest"), *args], env=env, out=out)
    return rc, out.getvalue()


def recorded(root: Path, name: str) -> str | None:
    p = root / "out" / name
    return p.read_text().strip() if p.exists() else None


def reset(root: Path) -> None:
    for p in (root / "out").iterdir():
        p.unlink()


def test_manifest_problems(tmp: Path) -> None:
    root = tree(tmp)
    rc, out = run(root, "--check")
    checks.expect(rc == 0 and "ok 8 tests" in out, f"a complete manifest passes --check: {rc} {out}")

    bad = (HEADER + MANIFEST.split(HEADER)[1]
           .replace("scripts/test-pass.sh\n", "scripts/test-pass.sh\nscripts/test-pass.sh\nscripts/test-gone.sh\n")
           .replace("scripts/test-fail.py\n", "scripts/test-fail.py   nosuch\n")
           .replace("scripts/test-image.py           image\n", "scripts/test-image.py\n")
           .replace("imessage/tests/test-slow-b.sh   slow\n", "imessage/tests/test-slow-b.sh   image\n"))
    (root / "scripts" / "tests.manifest").write_text(bad)
    (root / "scripts" / "test-new.py").write_text("")
    rc, out = run(root, "--check")
    checks.expect(rc == 2, f"a stale manifest exits 2: {rc}")
    for want in ("test-pass.sh is already listed", "test-gone.sh does not exist", "undeclared tag nosuch",
                 "test-image.py uses the Hermes image gate but is not tagged image",
                 "test-slow-b.sh is tagged image but never uses the Hermes image gate",
                 "scripts/test-new.py is not in the manifest"):
        checks.expect(want in out, f"--check reports {want!r}: {out}")
    rc, out = run(root, "scripts/test-pass.sh")
    checks.expect(rc == 2 and "out of date" in out, "a targeted run also refuses a stale manifest")

    rc, out = run(tree(tmp / "t2", "scripts/test-pass.sh\n", {"scripts/test-pass.sh": ""}), "--check")
    checks.expect(rc == 2 and "requirement tag docker is not declared" in out, f"requirement tags must be declared: {out}")


def test_selection(tmp: Path) -> None:
    root = tree(tmp)
    rc, out = run(root, "--list", "slow")
    checks.expect(rc == 0 and out.count("\n") == 2 and "test-pass" not in out, f"a substring selects: {out}")
    rc, out = run(root, "--list", "test-p*.sh")
    checks.expect(rc == 0 and "test-pass.sh" in out and out.count("\n") == 1, f"a glob on the name selects: {out}")
    rc, out = run(root, "--list", "--tag", "slow", "--exclude-tag", "docker")
    checks.expect(rc == 0 and out.count("\n") == 2, f"--tag selects: {out}")
    rc, out = run(root, "--list", "-x", "slow", "-x", "image", "-x", "docker", "-x", "macos")
    checks.expect(rc == 0 and out.count("\n") == 3, f"--exclude-tag leaves tests out: {out}")
    rc, out = run(root, "--list", "nothing-matches")
    checks.expect(rc == 2 and "no test in the manifest matches" in out, f"an unmatched pattern exits 2: {out}")
    rc, out = run(root, "--list", "--tag", "nosuch")
    checks.expect(rc == 2 and "unknown tag nosuch" in out, f"an unknown tag exits 2: {out}")
    rc, out = run(root, "--list", "--tag", "slow", "--tag", "docker")
    checks.expect(rc == 2 and "no tests selected" in out, f"an empty selection exits 2: {out}")
    rc, out = run(root, "--list", env=BARE)
    checks.expect("test-docker.sh" in out and "skip (needs Docker)" in out and "skip (needs macOS" in out,
                  f"--list shows the runner's skips: {out}")
    checks.expect("test-image.py --host-only" in out, f"--list shows the image test's arguments: {out}")


def test_run(tmp: Path) -> None:
    root = tree(tmp)
    logs = tmp / "logs"
    rc, out = run(root, "--log-root", str(logs))
    checks.expect(rc == 1, f"a failing test fails the run: {rc}")
    checks.expect("8 tests in" in out and "6 passed, 1 skipped, 1 failed" in out, f"summary counts: {out}")
    checks.expect("FAIL scripts/test-fail.py: exit 3" in out and "FAIL broken thing" in out and "line one" in out,
                  f"a failure shows its exit and its log: {out}")
    checks.expect("scripts/test-skip.sh: no widget here" in out, f"exit 77 is a skip, with its SKIP line: {out}")
    checks.expect(recorded(root, "pass") == "ran", f"a sh test runs with no arguments: {recorded(root, 'pass')}")
    checks.expect(recorded(root, "image") == "|1", f"an image test runs whole, with HERMES_IMAGE_REQUIRED=1: "
                  f"{recorded(root, 'image')}")
    checks.expect(recorded(root, "docker") == "" and recorded(root, "mac") == "", "docker and macos tests ran")
    latest = logs / "latest"
    checks.expect(latest.is_symlink() and (latest / "scripts__test-fail.py.log").is_file(), "logs under latest/")
    log = (latest / "scripts__test-fail.py.log").read_text()
    checks.expect(log.startswith(f"$ {sys.executable} scripts/test-fail.py\n"), f"the log names the command: {log}")
    times = json.loads((logs / "times.json").read_text())
    checks.expect(set(times) == set(TESTS) - {"scripts/test-skip.sh"}, f"times for every test that ran: {times}")

    reset(root)
    rc, out = run(root, "--log-root", str(logs), "-x", "slow", env=BARE)
    checks.expect(recorded(root, "docker") is None and recorded(root, "mac") is None,
                  "without Docker or macOS those tests do not start")
    checks.expect("scripts/test-docker.sh: needs Docker" in out and "scripts/test-mac.py: needs macOS" in out,
                  f"the runner's skips are listed: {out}")
    checks.expect(recorded(root, "image") == "--host-only|", f"no image: host part only: {recorded(root, 'image')}")
    checks.expect("Ran without their image part:\n  scripts/test-image.py" in out, f"and it says so: {out}")

    reset(root)
    rc, out = run(root, "--log-root", str(logs), "--host-only", "image")
    checks.expect(rc == 0 and recorded(root, "image") == "--host-only|", "--host-only passes --host-only")

    reset(root)
    rc, out = run(root, "--log-root", str(logs), "--strict", "image", "pass", env=NO_IMAGE)
    checks.expect(recorded(root, "image") == "|1", f"--strict runs the image part without the image: "
                  f"{recorded(root, 'image')}")
    rc, out = run(root, "--log-root", str(logs), "--strict", "skip", "pass")
    checks.expect(rc == 1 and "--- SKIP scripts/test-skip.sh" in out and "(--strict: skips fail)" in out,
                  f"--strict fails on a skip: {rc} {out}")
    rc, out = run(root, "--log-root", str(logs), "--strict", "docker", env=BARE)
    checks.expect(rc == 1, "--strict fails on a runner skip")
    rc, out = run(root, "--log-root", str(logs), "skip", "pass")
    checks.expect(rc == 0, f"without --strict a skip passes: {out}")

    runs = [p for p in logs.iterdir() if p.is_dir() and not p.is_symlink()]
    checks.expect(len(runs) <= rt.KEEP_RUNS, f"old runs are pruned to {rt.KEEP_RUNS}: {len(runs)}")


def test_parallel_and_order(tmp: Path) -> None:
    root = tree(tmp)
    logs = tmp / "logs"
    start = time.monotonic()
    rc, out = run(root, "--log-root", str(logs), "-j", "2", "slow")
    took = time.monotonic() - start
    checks.expect(rc == 0 and took < 1.9, f"two 1s tests with -j 2 take under 2s: {took:.1f}s")
    (logs / "times.json").write_text(json.dumps({"imessage/tests/test-slow-b.sh": 9, "scripts/test-pass.sh": 1}))
    rc, out = run(root, "--log-root", str(logs), "-j", "1", "slow", "pass")
    order = [line.split()[-1] for line in out.splitlines() if line.startswith("[")]
    checks.expect(order == ["imessage/tests/test-slow-a.sh", "imessage/tests/test-slow-b.sh", "scripts/test-pass.sh"],
                  f"a test with no time first, then longest first: {order}")


def test_timeout(tmp: Path) -> None:
    tests = {"scripts/test-hang.sh": "#!/bin/sh\nsleep 60 &\necho $! > \"$OUT/child\"\nwait\n"}
    root = tree(tmp, HEADER + "scripts/test-hang.sh\n", tests)
    start = time.monotonic()
    rc, out = run(root, "--log-root", str(tmp / "logs"), "--timeout", "1")
    checks.expect(rc == 1 and "timed out after 1s" in out and time.monotonic() - start < 15,
                  f"a hung test is stopped at the timeout: {rc} {out}")
    child = int(recorded(root, "child") or 0)
    gone = False
    for _ in range(20):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            gone = True
            break
        time.sleep(0.1)
    checks.expect(child and gone, f"the test's background child {child} is stopped too")


def main() -> int:
    for fn in (test_manifest_problems, test_selection, test_run, test_parallel_and_order, test_timeout):
        with tempfile.TemporaryDirectory(prefix="run-tests-") as tmp:
            fn(Path(tmp))
    rc, out = run(ROOT, "--check", env=FULL)
    checks.expect(rc == 0, f"scripts/tests.manifest matches the tests on disk: {out}")
    checks.finish("OK run-tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
