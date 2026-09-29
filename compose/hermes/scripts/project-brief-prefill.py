#!/usr/bin/env python3
"""Emit WebUI prefill messages from the active project's AGENTS.md.

Hermes WebUI (gateway mode) does not pass workspace as agent cwd, so native
AGENTS.md injection never runs for browser chat. This script is wired via
HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT and reads last_workspace.txt instead.

Stdout must be JSON: {"messages": [{"role": "system", "content": "..."}]}
or {"messages": []} when there is nothing to inject.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

HOME_WORKSPACE = "/opt/projects"
DEFAULT_STATE_DIR = "/opt/data/webui"
DEFAULT_MAX_CHARS = 8000
MANIFEST_NAME = ".index-manifest.json"
SKIP_NAMES = frozenset({"AGENTS.md", "INDEX.md", "README.md", "sources.yaml", MANIFEST_NAME})
SKIP_DIR_NAMES = frozenset({"templates", "applications", "artifacts", "drafts"})


def _emit(messages: list[dict]) -> None:
    sys.stdout.write(json.dumps({"messages": messages}, ensure_ascii=False))
    sys.stdout.write("\n")


def _strip_frontmatter(content: str) -> str:
    text = content.lstrip("\ufeff")
    if not text.startswith("---"):
        return text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            return "".join(lines[idx + 1 :]).lstrip("\n")
    return text


def _resolve_workspace(raw: str) -> Path | None:
    workspace = raw.strip()
    if not workspace:
        return None
    if workspace.rstrip("/") == HOME_WORKSPACE:
        return None

    test_root = os.environ.get("TEST_WORKSPACE_ROOT", "").strip()
    if test_root and workspace.startswith(HOME_WORKSPACE + "/"):
        rel = workspace[len(HOME_WORKSPACE) + 1 :]
        return Path(test_root).expanduser().resolve() / rel

    return Path(workspace).expanduser().resolve()


def _is_project_local_path(path: str) -> bool:
    """Host-absolute INDEX paths live on extra roots this container cannot see."""
    return bool(path) and not path.startswith("/")


def _file_entry(path: Path, root: Path) -> dict:
    st = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "path": path.relative_to(root).as_posix(),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "sha256": digest,
    }


def _iter_project_local_markdown(root: Path) -> list[Path]:
    """Walk only this project folder. Extra sources.yaml roots are host-side."""
    files: list[Path] = []
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        if path.name in SKIP_NAMES:
            continue
        if any(part in SKIP_DIR_NAMES for part in path.relative_to(root).parts[:-1]):
            continue
        files.append(path)
    return files


def _index_is_stale(root: Path) -> bool | None:
    """Stale-check project-local INDEX rows only.

    hermes-webui does not mount CONTEXT_EXTRA_ROOTS. Extra-root freshness is
    `make project-index-check` / doctor on the host.
    """
    manifest_path = root / MANIFEST_NAME
    index_path = root / "INDEX.md"
    if not manifest_path.is_file() or not index_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    recorded = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(recorded, list):
        return True

    recorded_local = [
        item
        for item in recorded
        if isinstance(item, dict) and _is_project_local_path(str(item.get("path") or ""))
    ]
    current = {}
    for path in _iter_project_local_markdown(root):
        entry = _file_entry(path, root)
        current[entry["path"]] = entry

    if len(recorded_local) != len(current):
        return True
    for item in recorded_local:
        key = item.get("path")
        if key not in current:
            return True
        cur = current[key]
        if (
            item.get("size") != cur["size"]
            or item.get("mtime_ns") != cur["mtime_ns"]
            or item.get("sha256") != cur["sha256"]
        ):
            return True
    return False


def _max_chars() -> int:
    raw = os.environ.get("HERMES_WEBUI_PREFILL_CONTEXT_MAX_CHARS", "").strip()
    try:
        value = int(raw or DEFAULT_MAX_CHARS)
    except ValueError:
        value = DEFAULT_MAX_CHARS
    return max(0, value)


def main() -> int:
    state_dir = Path(os.environ.get("HERMES_WEBUI_STATE_DIR", DEFAULT_STATE_DIR)).expanduser()
    last_file = state_dir / "last_workspace.txt"
    try:
        raw = last_file.read_text(encoding="utf-8")
    except OSError:
        _emit([])
        return 0

    root = _resolve_workspace(raw)
    if root is None or not root.is_dir():
        _emit([])
        return 0

    agents = root / "AGENTS.md"
    if not agents.is_file():
        _emit([])
        return 0

    try:
        body = _strip_frontmatter(agents.read_text(encoding="utf-8")).strip()
    except OSError as exc:
        print(f"project-brief-prefill: cannot read {agents}: {exc}", file=sys.stderr)
        _emit([])
        return 0

    if not body:
        _emit([])
        return 0

    stale = _index_is_stale(root)
    if stale is True:
        body = (
            f"{body}\n\n"
            "Note: INDEX.md may be out of date relative to files under this "
            "project folder; read source files directly rather than trusting "
            "the index. Extra source roots (absolute paths in INDEX.md) are "
            "not checked here."
        )

    limit = _max_chars()
    if limit and len(body) > limit:
        print(
            f"project-brief-prefill: AGENTS.md is {len(body)} chars "
            f"(limit {limit}); omitting brief rather than truncating.",
            file=sys.stderr,
        )
        _emit([])
        return 0

    _emit([{"role": "system", "content": body}])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
