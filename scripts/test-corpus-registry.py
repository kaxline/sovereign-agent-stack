#!/usr/bin/env python3
"""scripts/lib/corpus_registry.py: each subcommand, the invalid-file rule, and
corpus.sh create and list run against it in a stack copy with synthetic data.

    python3 scripts/test-corpus-registry.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
from testkit import expect  # noqa: E402

LIB = ROOT / "scripts" / "lib" / "corpus_registry.py"


def reg(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(LIB), str(path), *args], capture_output=True, text=True)


def rows(path: Path) -> list[dict]:
    return json.loads(path.read_text())["corpora"]


with tempfile.TemporaryDirectory() as tmp:
    t = Path(tmp)
    data = t / "data"
    (data / "inputs" / "hot").mkdir(parents=True)
    (data / "inputs" / ".hidden").mkdir()
    path = data / "corpora" / "registry.json"

    # ensure: creates the file and backfills WORKSPACE when its inputs folder exists.
    r = reg(path, "ensure", str(data), "", "m", "8")
    expect(r.returncode == 0 and r.stdout == "" and rows(path) == [], f"ensure with no WORKSPACE: {r}")
    r = reg(path, "ensure", str(data), "hot", "syn-embed", "768")
    expect(r.stdout == "backfilled:hot\n", f"ensure backfills hot: {r.stdout!r}")
    hot = rows(path)[0]
    expect(hot["slug"] == "hot" and hot["embedding_model"] == "syn-embed" and hot["embedding_dim"] == "768", hot)
    expect(hot["created_at"].endswith("Z"), "created_at is UTC")
    expect(reg(path, "ensure", str(data), "hot", "x", "1").stdout == "", "ensure backfills once")
    expect(reg(path, "ensure", str(data), "nodir", "x", "1").stdout == "", "ensure skips a WORKSPACE with no inputs folder")

    # add, has, dim, remove.
    expect(reg(path, "has", "alpha").returncode == 1, "has before add")
    reg(path, "add", "alpha", "syn-embed", "384")
    reg(path, "add", "alpha", "other", "1")
    expect([c["slug"] for c in rows(path)] == ["hot", "alpha"], "add appends once")
    expect(reg(path, "has", "alpha").returncode == 0, "has after add")
    expect(reg(path, "dim", "alpha").stdout == "384\n", "dim of alpha")
    expect(reg(path, "dim", "nope").stdout == "", "dim of a missing slug prints nothing")
    reg(path, "remove", "alpha")
    expect([c["slug"] for c in rows(path)] == ["hot"], "remove drops alpha")
    missing = t / "none" / "registry.json"
    expect(reg(missing, "remove", "x").returncode == 0 and not missing.exists(), "remove with no file writes nothing")
    expect(reg(missing, "has", "x").returncode == 1, "has with no file")

    # list: registry rows plus inputs folders, the active one marked.
    reg(path, "add", "alpha", "syn-embed", "384")
    (data / "inputs" / "alpha").mkdir()
    (data / "inputs" / "loose").mkdir()
    reg(path, "add", "gone", "", "")
    r = reg(path, "list", str(data), "hot")
    expect(r.stdout == (
        "  alpha  dim=384  model=syn-embed  inputs=yes\n"
        "  gone  dim=?  model=?  inputs=missing\n"
        "* hot  dim=768  model=syn-embed  inputs=yes\n"
        "  loose  dim=?  model=?  inputs=yes\n"
        "\nactive WORKSPACE=hot\n"
    ), f"list table: {r.stdout!r}")
    empty = t / "empty"
    expect(reg(empty / "registry.json", "list", str(empty), "").stdout == "(no corpora)\n", "list with nothing")

    # Entries that are not objects are skipped, not a crash.
    path.write_text(json.dumps({"corpora": [{"slug": "hot", "embedding_dim": "768"}, "junk", 3]}))
    r = reg(path, "has", "hot")
    expect(r.returncode == 0 and r.stderr == "", f"has skips non-object entries: {r}")
    reg(path, "add", "beta", "m", "1")
    expect([c["slug"] for c in rows(path)] == ["hot", "beta"], "add drops non-object entries")

    # An invalid file: every subcommand reads it as empty with a warning, and the
    # first write moves it to registry.json.bad instead of overwriting it.
    bad = path.with_name("registry.json.bad")
    for text in ("not json", '{"corpora": {}}', "[]"):
        path.write_text(text)
        bad.unlink(missing_ok=True)
        for args in (("has", "hot"), ("dim", "hot"), ("list", str(data), "")):
            r = reg(path, *args)
            expect(r.returncode in (0, 1) and "not a valid registry" in r.stderr, f"{args} on {text!r}: {r}")
        expect(path.read_text() == text and not bad.exists(), f"reads left {text!r} alone")
        r = reg(path, "add", "alpha", "m", "1")
        expect(r.returncode == 0 and "registry.json.bad" in r.stderr, f"add over {text!r}: {r}")
        expect(bad.read_text() == text, f"the invalid file {text!r} is kept as registry.json.bad")
        expect([c["slug"] for c in rows(path)] == ["alpha"], "add starts a new registry")
    path.write_text("not json")
    r = reg(path, "ensure", str(data), "", "", "")
    expect(rows(path) == [] and bad.read_text() == "not json", "ensure replaces an invalid file, keeping it")
    path.write_text("not json")
    reg(path, "remove", "x")
    expect(rows(path) == [] and bad.read_text() == "not json", "remove replaces an invalid file, keeping it")

    expect(reg(path, "nope").returncode == 2 and reg(path, "has").returncode == 2, "bad usage exits 2")

    # corpus.sh in a stack copy (its data root comes from ROOT/.env).
    stack = t / "stack"
    (stack / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "corpus.sh", stack / "scripts")
    for f in ("env.sh", "data-root.sh", "corpus_registry.py"):
        shutil.copy(ROOT / "scripts" / "lib" / f, stack / "scripts" / "lib")
    sdata = t / "sdata"
    (sdata / "inputs" / "hot").mkdir(parents=True)
    (stack / ".env").write_text(f"ASSISTANT_DATA_ROOT={sdata}\nWORKSPACE=hot\nEMBEDDING_MODEL=syn-embed\nEMBEDDING_DIM=768\n")

    def corpus(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(stack / "scripts" / "corpus.sh"), *args], capture_output=True, text=True)

    r = corpus("create", "alpha")
    expect(r.returncode == 0 and "Backfilled registry with existing WORKSPACE 'hot'" in r.stdout, f"corpus.sh create: {r}")
    r = corpus("list")
    expect(r.stdout == (
        "  alpha  dim=768  model=syn-embed  inputs=yes\n"
        "* hot  dim=768  model=syn-embed  inputs=yes\n"
        "\nactive WORKSPACE=hot\n"
    ), f"corpus.sh list: {r.stdout!r}")
    expect("<<'PY'" not in (ROOT / "scripts" / "corpus.sh").read_text().split("cmd_ingest")[0],
           "corpus.sh has an inline Python program before ingest again")

print("ok corpus registry")
