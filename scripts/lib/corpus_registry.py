#!/usr/bin/env python3
"""The corpus registry, <data root>/corpora/registry.json, for scripts/corpus.sh.

    corpus_registry.py REGISTRY ensure DATA_ROOT WORKSPACE MODEL DIM
    corpus_registry.py REGISTRY has SLUG          exit 0 when SLUG has a row
    corpus_registry.py REGISTRY add SLUG MODEL DIM
    corpus_registry.py REGISTRY remove SLUG
    corpus_registry.py REGISTRY dim SLUG          SLUG's embedding_dim
    corpus_registry.py REGISTRY list DATA_ROOT ACTIVE

The file is {"corpora": [{"slug", "embedding_model", "embedding_dim",
"created_at"}, ...]}. A file that is not JSON or not that shape is read as
empty, with a warning on stderr. The first write after that moves it to
registry.json.bad rather than overwriting it. Entries that are not objects are
skipped.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def warn(message: str) -> None:
    print(f"[corpus] WARNING: {message}", file=sys.stderr)


def load(path: Path) -> tuple[dict, bool]:
    """The registry and whether the file on disk is unusable."""
    if not path.is_file():
        return {"corpora": []}, False
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("corpora"), list):
        warn(f"{path} is not a valid registry; reading it as empty")
        return {"corpora": []}, True
    data["corpora"] = [c for c in data["corpora"] if isinstance(c, dict)]
    return data, False


def save(path: Path, data: dict, bad: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if bad and path.is_file():
        kept = path.with_name(path.name + ".bad")
        path.replace(kept)
        warn(f"kept the invalid registry as {kept} and started a new one")
    path.write_text(json.dumps(data, indent=2) + "\n")


def find(data: dict, slug: str) -> dict | None:
    return next((c for c in data["corpora"] if c.get("slug") == slug), None)


def new_row(slug: str, model: str, dim: str) -> dict:
    return {
        "slug": slug,
        "embedding_model": model,
        "embedding_dim": dim,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def ensure(path: Path, data_root: str, workspace: str, model: str, dim: str) -> int:
    """Creates the file, and adds WORKSPACE when its inputs folder exists but it has no row."""
    data, bad = load(path)
    if workspace and find(data, workspace) is None and (Path(data_root) / "inputs" / workspace).is_dir():
        data["corpora"].append(new_row(workspace, model, dim))
        save(path, data, bad)
        print(f"backfilled:{workspace}")
    elif bad or not path.is_file():
        save(path, data, bad)
    return 0


def has(path: Path, slug: str) -> int:
    data, _ = load(path)
    return 0 if find(data, slug) is not None else 1


def add(path: Path, slug: str, model: str, dim: str) -> int:
    data, bad = load(path)
    if find(data, slug) is None:
        data["corpora"].append(new_row(slug, model, dim))
        save(path, data, bad)
    return 0


def remove(path: Path, slug: str) -> int:
    if not path.is_file():
        return 0
    data, bad = load(path)
    data["corpora"] = [c for c in data["corpora"] if c.get("slug") != slug]
    save(path, data, bad)
    return 0


def dim(path: Path, slug: str) -> int:
    row = find(load(path)[0], slug)
    if row is not None:
        print(row.get("embedding_dim") or "")
    return 0


def list_corpora(path: Path, data_root: str, active: str) -> int:
    """One line per corpus in the registry or under inputs/, * on ACTIVE."""
    inputs = Path(data_root) / "inputs"
    rows = {c["slug"]: c for c in load(path)[0]["corpora"] if c.get("slug")}
    slugs = set(rows)
    if inputs.is_dir():
        try:
            slugs.update(p.name for p in inputs.iterdir() if p.is_dir() and not p.name.startswith("."))
        except PermissionError:
            pass
    if not slugs:
        print("(no corpora)")
        return 0
    for slug in sorted(slugs):
        mark = "*" if slug == active else " "
        row = rows.get(slug, {})
        try:
            on_disk = "yes" if (inputs / slug).is_dir() else "missing"
        except PermissionError:
            on_disk = "?"
        print(f"{mark} {slug}  dim={row.get('embedding_dim') or '?'}  model={row.get('embedding_model') or '?'}  inputs={on_disk}")
    if active:
        print(f"\nactive WORKSPACE={active}")
    return 0


COMMANDS = {
    "ensure": (ensure, 4),
    "has": (has, 1),
    "add": (add, 3),
    "remove": (remove, 1),
    "dim": (dim, 1),
    "list": (list_corpora, 2),
}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in COMMANDS or len(argv) - 2 != COMMANDS[argv[1]][1]:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    func, _ = COMMANDS[argv[1]]
    return func(Path(argv[0]), *argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
