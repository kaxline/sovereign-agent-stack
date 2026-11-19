#!/usr/bin/env python3
"""Emit WebUI prefill messages from the active project's AGENTS.md.

Hermes WebUI (gateway mode) does not pass workspace as agent cwd, so native
AGENTS.md injection never runs for browser chat. This script is wired via
HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT and reads last_workspace.txt instead.

Stdout must be JSON: {"messages": [{"role": "system", "content": "..."}]}
or {"messages": []} when there is nothing to inject.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Compose mounts scripts/lib/project_sources.py beside this script under
# /bootstrap. From a repo checkout (tests), fall back to scripts/lib.
_HERE = Path(__file__).resolve().parent
_REPO_LIB = _HERE.parent.parent.parent / "scripts" / "lib"
if not (_HERE / "project_sources.py").is_file() and _REPO_LIB.is_dir():
    sys.path.insert(0, str(_REPO_LIB))

import project_sources as ps  # noqa: E402

HOME_WORKSPACE = "/opt/projects"
DEFAULT_STATE_DIR = "/opt/data/webui"
DEFAULT_MAX_CHARS = 8000


def _emit(messages: list[dict]) -> None:
    sys.stdout.write(json.dumps({"messages": messages}, ensure_ascii=False))
    sys.stdout.write("\n")


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


def _index_is_stale(root: Path) -> bool | None:
    """Stale-check project-local INDEX rows only; None when there is no INDEX.

    hermes-webui does not mount CONTEXT_EXTRA_ROOTS. Extra-root freshness is
    `make project-index-check` / doctor on the host.
    """
    if not (root / ps.MANIFEST_NAME).is_file() or not (root / ps.INDEX_NAME).is_file():
        return None
    try:
        return not ps.catalog_is_fresh(root, local_only=True)
    except (ps.SourcesError, OSError) as exc:
        print(
            f"project-brief-prefill: cannot check {root / ps.INDEX_NAME}: {exc}",
            file=sys.stderr,
        )
        return None


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
        body = ps.strip_frontmatter(agents.read_text(encoding="utf-8")).strip()
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
