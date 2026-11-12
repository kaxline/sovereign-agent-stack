#!/usr/bin/env python3
"""Run the tests listed in scripts/tests.manifest, several at a time.

    python3 scripts/run-tests.py                  every test this machine can run
    python3 scripts/run-tests.py mail bitwarden   tests whose path contains a pattern (or matches a glob)
    python3 scripts/run-tests.py --tag image      tests with every given tag
    python3 scripts/run-tests.py --list           what would run, and how, without running it
    python3 scripts/run-tests.py --check          only check the manifest

Each test runs in its own process group with stdin closed and its output in
.test-runs/<time>/<name>.log. A test exits 0 to pass, 77 to skip (printing a
line that starts with SKIP to say why) and anything else to fail.

The manifest tags say what a test needs. A docker test is skipped without a
running Docker daemon, and a macos test off macOS or without swiftc. An image
test runs its image part when the pinned Hermes image is present, with
HERMES_IMAGE_REQUIRED=1, and only its host part (--host-only) otherwise.
--strict turns every skip into a failure and requires the image.

Exit status: 0 when nothing failed, 1 when a test failed (or skipped under
--strict), 2 for a bad manifest or selection, 130 when interrupted.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "scripts" / "tests.manifest"
TEST_GLOBS = ("scripts/test-*", "imessage/tests/test-*")
REQUIREMENT_TAGS = ("docker", "image", "macos")
SKIP_EXIT = 77
GATE_RE = re.compile(r"hermes_image\.(maybe_run|image_gate|run_driver)|hermes_image_gate |\brun_in_image\(")
KEEP_RUNS = 10


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class Test:
    path: str
    tags: frozenset
    line: int


@dataclass
class Manifest:
    tags: dict
    tests: list


def parse_manifest(text: str, where: str = "tests.manifest") -> Manifest:
    """``@tag NAME description`` declares a tag; ``PATH TAG...`` lists a test."""
    tags: dict = {}
    tests: list = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("@tag"):
            parts = line.split(None, 2)
            if len(parts) < 3:
                raise ManifestError(f"{where}:{n}: @tag needs a name and a description")
            tags[parts[1]] = parts[2]
            continue
        path, *t = line.split()
        tests.append(Test(path, frozenset(t), n))
    return Manifest(tags, tests)


def manifest_problems(manifest: Manifest, root: Path) -> list:
    """Everything that would make the manifest disagree with the tests on disk."""
    problems = []
    for tag in REQUIREMENT_TAGS:
        if tag not in manifest.tags:
            problems.append(f"requirement tag {tag} is not declared with @tag")
    seen: dict = {}
    for t in manifest.tests:
        if t.path in seen:
            problems.append(f"line {t.line}: {t.path} is already listed on line {seen[t.path]}")
        seen[t.path] = t.line
        for tag in sorted(t.tags - set(manifest.tags)):
            problems.append(f"line {t.line}: {t.path} has undeclared tag {tag}")
        f = root / t.path
        if not f.is_file():
            problems.append(f"line {t.line}: {t.path} does not exist")
            continue
        gated = bool(GATE_RE.search(f.read_text(errors="replace")))
        if gated and "image" not in t.tags:
            problems.append(f"line {t.line}: {t.path} uses the Hermes image gate but is not tagged image")
        if "image" in t.tags and not gated:
            problems.append(f"line {t.line}: {t.path} is tagged image but never uses the Hermes image gate")
    on_disk = sorted({str(p.relative_to(root)) for g in TEST_GLOBS for p in root.glob(g) if p.is_file()})
    for path in on_disk:
        if path not in seen:
            problems.append(f"{path} is not in the manifest")
    return problems


@dataclass(frozen=True)
class Env:
    docker: bool
    image: str  # nousresearch/hermes-agent:<tag>
    image_present: bool
    macos: bool


def detect_env(root: Path) -> Env:
    sys.path.insert(0, str(root / "scripts" / "lib"))
    import hermes_image  # noqa: PLC0415

    image = f"nousresearch/hermes-agent:{hermes_image.image_tag()}"
    docker = False
    if shutil.which("docker"):
        try:
            docker = subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0
        except subprocess.TimeoutExpired:
            docker = False
    present = docker and subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
    macos = sys.platform == "darwin" and shutil.which("swiftc") is not None
    return Env(docker, image, present, macos)


@dataclass
class Job:
    test: Test
    argv: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    skip: str = ""  # set when the runner skips the test without starting it
    note: str = ""


def interpreter(path: Path) -> list:
    if path.suffix == ".py":
        return [sys.executable]
    with path.open(errors="replace") as fh:
        first = fh.readline()
    return ["bash"] if "bash" in first else ["sh"]


def plan(test: Test, env: Env, root: Path, host_only: bool, strict: bool) -> Job:
    job = Job(test, interpreter(root / test.path) + [test.path])
    if "macos" in test.tags and not env.macos:
        job.skip = "needs macOS with swiftc"
    elif "docker" in test.tags and not env.docker:
        job.skip = "needs Docker"
    elif "image" in test.tags:
        if host_only:
            job.argv.append("--host-only")
            job.note = "host part only (--host-only)"
        elif env.image_present or strict:
            job.env["HERMES_IMAGE_REQUIRED"] = "1"
        else:
            job.argv.append("--host-only")
            job.note = f"host part only ({env.image} not present)"
    return job


@dataclass
class Result:
    job: Job
    status: str  # PASS, FAIL or SKIP
    seconds: float
    log: Path
    reason: str = ""


def log_name(path: str) -> str:
    return path.replace("/", "__") + ".log"


def skip_reason(log: Path) -> str:
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        if line.startswith("SKIP"):
            return line[4:].strip(" :")
    return f"exit {SKIP_EXIT}"


class Runner:
    def __init__(self, root: Path, logdir: Path, timeout: float, out=sys.stdout):
        self.root = root
        self.logdir = logdir
        self.timeout = timeout
        self.out = out
        self.lock = threading.Lock()
        self.running: dict = {}
        self.stopping = threading.Event()

    def run(self, job: Job) -> Result:
        log = self.logdir / log_name(job.test.path)
        if job.skip:
            log.write_text(f"SKIP {job.skip} (not started)\n")
            return Result(job, "SKIP", 0.0, log, job.skip)
        if self.stopping.is_set():
            log.write_text("not started: the run was interrupted\n")
            return Result(job, "FAIL", 0.0, log, "not started (interrupted)")
        env = {**os.environ, **job.env}
        start = time.monotonic()
        with log.open("wb") as fh:
            fh.write(f"$ {' '.join(job.argv)}\n".encode())
            fh.flush()
            proc = subprocess.Popen(job.argv, cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
            with self.lock:
                self.running[proc.pid] = proc
            try:
                rc = proc.wait(timeout=self.timeout)
                reason = ""
            except subprocess.TimeoutExpired:
                stop_group(proc)
                rc, reason = -1, f"timed out after {self.timeout:.0f}s"
            finally:
                with self.lock:
                    self.running.pop(proc.pid, None)
        seconds = time.monotonic() - start
        if self.stopping.is_set() and rc != 0:
            return Result(job, "FAIL", seconds, log, "interrupted")
        if rc == 0:
            return Result(job, "PASS", seconds, log)
        if rc == SKIP_EXIT:
            return Result(job, "SKIP", seconds, log, skip_reason(log))
        return Result(job, "FAIL", seconds, log, reason or f"exit {rc}")

    def stop(self) -> None:
        self.stopping.set()
        with self.lock:
            procs = list(self.running.values())
        for proc in procs:
            stop_group(proc)


def stop_group(proc: subprocess.Popen) -> None:
    """SIGTERM the test's own process group (its traps clean up), then SIGKILL."""
    for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def select(manifest: Manifest, patterns: list, tags: list, exclude: list) -> list:
    unknown = sorted(set(tags + exclude) - set(manifest.tags))
    if unknown:
        raise ManifestError(f"unknown tag {', '.join(unknown)} (declared: {', '.join(sorted(manifest.tags))})")
    chosen = []
    for t in manifest.tests:
        if not set(tags) <= t.tags or t.tags & set(exclude):
            continue
        if patterns and not any(matches(t.path, p) for p in patterns):
            continue
        chosen.append(t)
    for p in patterns:
        if not any(matches(t.path, p) for t in manifest.tests):
            raise ManifestError(f"no test in the manifest matches {p!r}")
    return chosen


def matches(path: str, pattern: str) -> bool:
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(Path(path).name, pattern)
    return pattern in path


def load_times(path: Path) -> dict:
    try:
        return {k: float(v) for k, v in json.loads(path.read_text()).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_times(path: Path, times: dict, results: list) -> None:
    for r in results:
        if r.status != "SKIP" and r.reason not in ("interrupted", "not started (interrupted)"):
            times[r.job.test.path] = round(r.seconds, 1)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(times, indent=1, sort_keys=True) + "\n")
    tmp.replace(path)


def new_logdir(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    logdir = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=base))
    latest = base / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(logdir.name)
    except OSError:
        pass
    runs = sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink())
    for old in runs[:-KEEP_RUNS]:
        shutil.rmtree(old, ignore_errors=True)
    return logdir


def describe(job: Job) -> str:
    if job.skip:
        return f"skip ({job.skip})"
    extra = " ".join(f"{k}={v}" for k, v in job.env.items())
    return " ".join(x for x in (extra, " ".join(job.argv)) if x)


def report(results: list, strict: bool, elapsed: float, tail: int, out) -> int:
    failed = [r for r in results if r.status == "FAIL"]
    skipped = [r for r in results if r.status == "SKIP"]
    partial = [r for r in results if r.status == "PASS" and r.job.note]
    for r in failed + (skipped if strict else []):
        out.write(f"\n--- {r.status} {r.job.test.path}: {r.reason} (log: {r.log})\n")
        lines = r.log.read_text(errors="replace").splitlines()
        shown = lines if tail <= 0 else lines[-tail:]
        if len(shown) < len(lines):
            out.write(f"[last {len(shown)} of {len(lines)} lines]\n")
        out.write("\n".join(shown) + "\n")
    if skipped:
        out.write("\nSkipped:\n" + "".join(f"  {r.job.test.path}: {r.reason}\n" for r in skipped))
    if partial:
        out.write("\nRan without their image part:\n" + "".join(f"  {r.job.test.path}\n" for r in partial))
    passed = len(results) - len(failed) - len(skipped)
    out.write(f"\n{len(results)} tests in {elapsed:.0f}s: {passed} passed, {len(skipped)} skipped, "
              f"{len(failed)} failed{' (--strict: skips fail)' if strict and skipped else ''}\n")
    out.flush()
    return 1 if failed or (strict and skipped) else 0


def parse_args(argv: list) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("patterns", nargs="*", help="substring of a test path, or a glob on its path or name")
    ap.add_argument("-t", "--tag", action="append", default=[], help="only tests with this tag (repeatable)")
    ap.add_argument("-x", "--exclude-tag", action="append", default=[], help="leave out tests with this tag")
    ap.add_argument("-j", "--jobs", type=int, default=max(2, min(8, (os.cpu_count() or 4) // 2)),
                    help="tests to run at once (default: half the CPUs, 2 to 8)")
    ap.add_argument("--host-only", action="store_true", help="never run the image part of image tests")
    ap.add_argument("--strict", action="store_true", help="a skipped test fails the run; image tests need the image")
    ap.add_argument("--timeout", type=float, default=1200, help="seconds before a test is stopped (default 1200)")
    ap.add_argument("--tail", type=int, default=40, help="log lines shown per failure; 0 shows the whole log")
    ap.add_argument("--list", action="store_true", help="show what would run, and how, then stop")
    ap.add_argument("--check", action="store_true", help="check the manifest against the tests on disk, then stop")
    ap.add_argument("--manifest", type=Path, default=MANIFEST, help=argparse.SUPPRESS)
    ap.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    ap.add_argument("--log-root", type=Path, default=None, help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def main(argv: list | None = None, env: Env | None = None, out=sys.stdout) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    root = args.root.resolve()
    try:
        manifest = parse_manifest(args.manifest.read_text(), str(args.manifest))
        problems = manifest_problems(manifest, root)
        if problems:
            raise ManifestError(f"{args.manifest} is out of date:\n  " + "\n  ".join(problems))
        if args.check:
            out.write(f"ok {len(manifest.tests)} tests in {args.manifest.name}\n")
            return 0
        tests = select(manifest, args.patterns, args.tag, args.exclude_tag)
    except (ManifestError, OSError) as e:
        out.write(f"run-tests: {e}\n")
        return 2
    if not tests:
        out.write("run-tests: no tests selected\n")
        return 2

    env = env or detect_env(root)
    jobs = [plan(t, env, root, args.host_only, args.strict) for t in tests]
    if args.list:
        for j in jobs:
            out.write(f"{j.test.path:<52} {' '.join(sorted(j.test.tags)):<24} {describe(j)}\n")
        return 0

    log_root = args.log_root or root / ".test-runs"
    logdir = new_logdir(log_root)
    times_file = log_root / "times.json"
    times = load_times(times_file)
    # Longest first, from the last run's times; a test with no time yet goes first.
    jobs.sort(key=lambda j: -times.get(j.test.path, float("inf")))
    out.write(f"{len(jobs)} tests, {args.jobs} at a time, logs in {logdir}\n"
              f"Docker: {'yes' if env.docker else 'no'}, image {env.image}: "
              f"{'present' if env.image_present else 'absent'}, macOS with swiftc: {'yes' if env.macos else 'no'}\n")
    out.flush()

    runner = Runner(root, logdir, args.timeout, out)
    results: list = []
    start = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=max(1, args.jobs))
    pending: set = set()
    width = len(str(len(jobs)))

    def finished(f: Future) -> None:
        r = f.result()
        results.append(r)
        extra = r.reason or r.job.note
        out.write(f"[{len(results):>{width}}/{len(jobs)}] {r.status} {r.seconds:6.1f}s  {r.job.test.path}"
                  f"{'  (' + extra + ')' if extra else ''}\n")
        out.flush()

    interrupted = False
    try:
        pending = {pool.submit(runner.run, j) for j in jobs}
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for f in done:
                finished(f)
    except KeyboardInterrupt:
        interrupted = True
        out.write("\ninterrupted: stopping running tests\n")
        runner.stop()
        for f in pending:
            finished(f)
    finally:
        pool.shutdown(wait=True)
    save_times(times_file, times, results)
    rc = report(results, args.strict, time.monotonic() - start, args.tail, out)
    return 130 if interrupted else rc


if __name__ == "__main__":
    raise SystemExit(main())
