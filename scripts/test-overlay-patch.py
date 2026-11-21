#!/usr/bin/env python3
"""Checks compose/hermes/overlay_patch.py, the helper every patch-*.py overlay uses.

Host-only and stdlib-only. Each case runs a small patch script in a subprocess,
the way the cont-init runners run the real ones, against files in a temp dir:
a missing or duplicate anchor exits non-zero and writes nothing (in any file),
a variant tuple picks the shape present, a present marker prints "already
applied", and optional=True prints a skip and exits 0.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERMES = ROOT / "compose" / "hermes"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok' if ok else 'FAIL'} {name}")
    if not ok:
        failures.append(f"{name}: {detail}")


def run(tmp: Path, body: str) -> subprocess.CompletedProcess:
    script = tmp / "patch-demo.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(HERMES)!r})\n"
        "from pathlib import Path\n"
        "from overlay_patch import FileEdit, edit, edit_files, install, one_of\n"
        f"T = Path({str(tmp)!r})\n"
        "TAG = '[patch-demo]'\n"
        "MARKER = '# assistant-stack: demo'\n" + textwrap.dedent(body))
    return subprocess.run([sys.executable, str(script)], capture_output=True, text=True)


def case(body: str, files: dict[str, str]) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        for name, text in files.items():
            (tmp / name).write_text(text)
        out = run(tmp, body)
        return out, {name: (tmp / name).read_text() for name in files}


A = "def a():\n    return 1\n"
B = "def b():\n    return 2\n"

# Missing anchor in the second file: exit 1, message names overlay and anchor, nothing written.
out, after = case("""
    edit_files([FileEdit(T / "a.py", [("return 1\\n", "return 1  # x\\n")]),
                FileEdit(T / "b.py", [("return 3\\n", "return 4\\n")])], MARKER, TAG)
""", {"a.py": A, "b.py": B})
check("missing anchor exits non-zero", out.returncode == 1, out.stdout + out.stderr)
check("missing anchor message names overlay and anchor",
      "[patch-demo] anchor missing in" in out.stderr and "'return 3'" in out.stderr and "not patched" in out.stderr,
      out.stderr)
check("missing anchor writes no file", after == {"a.py": A, "b.py": B}, repr(after))

# Duplicate anchor.
out, after = case("""
    edit(T / "a.py", [("return", "yield")], MARKER, TAG)
""", {"a.py": A + A})
check("duplicate anchor exits non-zero", out.returncode == 1 and "found 2" in out.stderr, out.stdout + out.stderr)
check("duplicate anchor writes nothing", after["a.py"] == A + A)

# Variant tuple: the second shape is the one present.
out, after = case("""
    edit(T / "a.py", [one_of(("def a(x):\\n", "def a(x, y=0):\\n"),
                             ("def a():\\n", "def a(y=0):  " + MARKER + "\\n"))], MARKER, TAG, label="a sig")
""", {"a.py": A})
check("variant tuple applies the present shape",
      out.returncode == 0 and after["a.py"].startswith("def a(y=0):  # assistant-stack: demo\n"), out.stdout + out.stderr)
check("applied line uses the label", "[patch-demo] patched a sig" in out.stdout, out.stdout)

# Neither variant present.
out, _ = case("""
    edit(T / "a.py", [one_of(("x\\n", "y\\n"), ("z\\n", "w\\n"))], MARKER, TAG)
""", {"a.py": A})
check("no variant present exits non-zero", out.returncode == 1 and "other shape" in out.stderr, out.stderr)

# Marker present: already applied, file untouched even though the anchor is gone.
out, after = case("""
    edit(T / "a.py", [("return 9\\n", "return 8\\n")], MARKER, TAG, label="a body")
""", {"a.py": A + "# assistant-stack: demo\n"})
check("marker present prints already applied",
      out.returncode == 0 and "[patch-demo] already applied (a body)" in out.stdout, out.stdout + out.stderr)

# optional=True: skip line, exit 0, nothing written.
out, after = case("""
    ok = edit(T / "a.py", [("return 9\\n", "return 8\\n")], MARKER, TAG, optional=True)
    print("result", ok)
""", {"a.py": A})
check("optional missing anchor exits 0", out.returncode == 0, out.stdout + out.stderr)
check("optional prints a skip and returns False",
      "[patch-demo] skipped: anchor missing in" in out.stdout and "result False" in out.stdout, out.stdout)
check("optional writes nothing", after["a.py"] == A)

# Missing target file.
out, _ = case("""
    edit(T / "gone.py", [("a", "b")], MARKER, TAG)
""", {})
check("missing target exits non-zero", out.returncode == 1 and "target missing" in out.stderr, out.stderr)

# requires, append, install.
out, after = case("""
    edit(T / "a.py", [], MARKER, TAG, append=MARKER + "\\nX = 1\\n", requires=["def a():"])
    install(T / "src.py", T / "dst.py", TAG)
""", {"a.py": A, "src.py": "S = 1\n"})
check("append and install apply", out.returncode == 0 and after["a.py"].endswith("# assistant-stack: demo\nX = 1\n")
      and "[patch-demo] installed" in out.stdout, out.stdout + out.stderr)
out, _ = case("""
    edit(T / "a.py", [], MARKER, TAG, append="X = 1\\n", requires=["def c():"])
""", {"a.py": A})
check("missing required name exits non-zero", out.returncode == 1 and "'def c():'" in out.stderr, out.stderr)
out, after = case("""
    edit_files([FileEdit(T / "a.py", [("return 1\\n", "return 1  " + MARKER + "\\n")]),
                FileEdit(T / "b.py", requires=["def b():"])], MARKER, TAG)
""", {"a.py": A, "b.py": B})
check("a requires-only FileEdit is a check: no write, no line",
      out.returncode == 0 and after["b.py"] == B and "b.py" not in out.stdout, out.stdout + out.stderr)
out, after = case("""
    edit_files([FileEdit(T / "a.py", [("def a():\\n", "def a():  # one\\n")], label="one", marker="# one"),
                FileEdit(T / "a.py", [("return 1\\n", "return 2  # two\\n")], label="two", marker="# two")],
               MARKER, TAG)
""", {"a.py": A})
check("FileEdits on one path chain into one write",
      after["a.py"] == "def a():  # one\n    return 2  # two\n" and "patched one" in out.stdout
      and "patched two" in out.stdout, out.stdout + out.stderr + repr(after))
out, _ = case("""
    install(T / "nope.py", T / "dst.py", TAG)
""", {})
check("missing install source exits non-zero", out.returncode == 1 and "source missing" in out.stderr, out.stderr)

compose = (ROOT / "docker-compose.yml").read_text()
check("docker-compose.yml mounts overlay_patch.py",
      "./compose/hermes/overlay_patch.py:/bootstrap/overlay_patch.py:ro" in compose)

if failures:
    print("\n".join(failures), file=sys.stderr)
    sys.exit(1)
print("PASS test-overlay-patch")
