"""Shared plumbing for the Hermes overlay scripts (compose/hermes/patch-*.py).

Stack-only, not upstream. Bind-mounted at /bootstrap/overlay_patch.py beside
the patch scripts, which import it from their own directory.

``install`` copies an overlay module into the Hermes tree. ``edit`` applies a
list of text edits to one upstream file, ``edit_files`` to several at once.
Every anchor in every file is checked before anything is written, so a moved
anchor leaves Hermes as shipped. A file that already holds the overlay's
marker is left alone and reported as "already applied".

An edit is ``(old, new)`` (``old`` must occur exactly once), ``(old, new,
count)``, or ``one_of((old, new), ...)`` for an anchor whose upstream text
differs between the supported Hermes tags; the first variant whose count
matches is used. Edits apply in order, each to the text the previous one left.

A missing anchor or target file raises SystemExit (exit 1) with one message
shape naming the overlay and the anchor. The cont-init runners turn that exit
into a ``warn:`` line and the container still starts (docs/contracts/upstreams.md).
``optional=True`` prints the same message as a skip and returns False instead.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import NamedTuple, Sequence

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))


class one_of(tuple):
    """Variants of one anchor: (old, new) pairs, the first whose count matches wins."""

    def __new__(cls, *pairs: tuple[str, str], count: int = 1):
        self = super().__new__(cls, pairs)
        self.count = count
        return self


class FileEdit(NamedTuple):
    path: Path
    edits: Sequence = ()
    label: str | None = None
    append: str = ""
    requires: Sequence[str] = ()


class _Missing(Exception):
    pass


def _short(anchor: str) -> str:
    line = next((ln.strip() for ln in anchor.splitlines() if ln.strip()), anchor.strip())
    return repr(line[:72])


def fail(tag: str, message: str, optional: bool = False) -> bool:
    """Report an overlay that cannot apply: raise, or with *optional* print a skip and return False."""
    if optional:
        print(f"{tag} skipped: {message}; not patched")
        return False
    raise SystemExit(f"{tag} {message}; not patched. Hermes image may have changed: update this patch.")


def install(src: Path, dst: Path, tag: str, optional: bool = False) -> bool:
    """Copy the overlay module *src* (under /bootstrap) to *dst* in the Hermes tree."""
    if not Path(src).is_file():
        return fail(tag, f"source missing: {src}", optional)
    shutil.copyfile(src, dst)
    print(f"{tag} installed {dst}")
    return True


def _apply(path: Path, text: str, f: FileEdit) -> str:
    for needle in f.requires:
        if needle not in text:
            raise _Missing(f"anchor missing in {path}: expected {_short(needle)}, found 0")
    for e in f.edits:
        variants, count = (e, e.count) if isinstance(e, one_of) else (((e[0], e[1]),), e[2] if len(e) > 2 else 1)
        for old, new in variants:
            found = text.count(old)
            if found == count:
                text = text.replace(old, new)
                break
        else:
            found = text.count(variants[0][0])
            shapes = f" (or {len(variants) - 1} other shape(s))" if len(variants) > 1 else ""
            raise _Missing(f"anchor missing in {path}: expected {count} x {_short(variants[0][0])}{shapes}, found {found}")
    if f.append:
        text = text.rstrip("\n") + "\n" + f.append
    return text


def edit_files(files: Sequence[FileEdit], marker: str, tag: str, optional: bool = False) -> bool:
    """Apply each FileEdit, checking every file and anchor before writing any of them."""
    pending: list[tuple[FileEdit, str | None]] = []
    for f in files:
        path = Path(f.path)
        if not path.is_file():
            return fail(tag, f"target missing: {path}", optional)
        text = path.read_text(encoding="utf-8")
        if marker in text:
            pending.append((f, None))
            continue
        try:
            pending.append((f, _apply(path, text, f)))
        except _Missing as exc:
            return fail(tag, str(exc), optional)
    for f, text in pending:
        label = f.label or str(f.path)
        if text is None:
            print(f"{tag} already applied ({label})")
        else:
            Path(f.path).write_text(text, encoding="utf-8")
            print(f"{tag} patched {label}")
    return True


def edit(path: Path, edits: Sequence, marker: str, tag: str, optional: bool = False, *,
         label: str | None = None, append: str = "", requires: Sequence[str] = ()) -> bool:
    """Apply *edits* to one file once; *marker* in the file means it is already applied."""
    return edit_files([FileEdit(path, edits, label, append, requires)], marker, tag, optional)
