#!/usr/bin/env python3
"""The WebUI prefill keeps a generated INDEX current, agreeing with project_sources.

compose/hermes/scripts/project-brief-prefill.py runs inside hermes-webui. When
the project's files no longer match .index-manifest.json it rewrites INDEX.md
if every source is inside the project, and otherwise appends "INDEX.md may be
out of date" to the project brief. Each case builds a scratch project,
indexes it with scripts/lib/project_sources.py (what `make project-index` runs)
and runs the prefill as WebUI does, with HERMES_WEBUI_STATE_DIR pointing at a
last_workspace.txt for that project.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LIB = HERE / "lib"
PREFILL = ROOT / "compose" / "hermes" / "scripts" / "project-brief-prefill.py"

if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import project_sources as ps  # noqa: E402

from testkit import expect  # noqa: E402

STALE_NOTE = "INDEX.md may be out of date"
BRIEF = "# Brief\n\nProject brief body.\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run_prefill(project: Path, script: Path = PREFILL) -> str:
    with tempfile.TemporaryDirectory() as state:
        Path(state, "last_workspace.txt").write_text(str(project), encoding="utf-8")
        env = dict(os.environ, HERMES_WEBUI_STATE_DIR=state)
        env.pop("TEST_WORKSPACE_ROOT", None)
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    expect(proc.returncode == 0, f"prefill exited {proc.returncode}: {proc.stderr}")
    payload = json.loads(proc.stdout)
    messages = payload["messages"]
    expect(len(messages) == 1, f"expected one brief message, got {payload}")
    content = messages[0]["content"]
    expect(content.startswith("# Brief"), f"brief missing: {content!r}")
    return content


def _txt_and_hidden_project(root: Path) -> Path:
    project = root / "txt-project"
    _write(project / "AGENTS.md", BRIEF)
    _write(
        project / "sources.yaml",
        "version: 1\n"
        "sources:\n"
        "  - id: project\n"
        "    path: .\n"
        "    retrieval: files\n"
        '    include: ["**/*.md", "**/*.txt"]\n',
    )
    _write(project / "notes" / "plan.md", "# Plan\n")
    _write(project / "notes" / "transcript.txt", "call transcript\n")
    _write(project / ".hidden.md", "# Hidden\n")
    _write(project / ".obsidian" / "workspace.md", "# Obsidian state\n")
    ps.write_index(project)
    return project


def test_txt_and_hidden_sources_are_fresh() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = _txt_and_hidden_project(Path(tmp))
        indexed = {item.path for item in ps.build_catalog(project)[0]}
        expect(
            indexed == {"notes/plan.md", "notes/transcript.txt"},
            f"generator indexed {sorted(indexed)}",
        )
        content = _run_prefill(project)
        expect(STALE_NOTE not in content, "fresh .txt project reported stale")

        _write(project / "notes" / "transcript.txt", "edited transcript\n")
        content = _run_prefill(project)
        expect(STALE_NOTE not in content, "refreshed .txt project still noted stale")
        expect(ps.catalog_is_fresh(project), "edited .txt source not refreshed")


def test_markdown_only_project_unchanged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "md-project"
        _write(project / "AGENTS.md", BRIEF)
        _write(project / "notes" / "a.md", "# A\n")
        _write(project / "drafts" / "skip.md", "# Draft\n")

        content = _run_prefill(project)
        expect(STALE_NOTE not in content, "no manifest yet should add no note")

        ps.write_index(project)
        content = _run_prefill(project)
        expect(STALE_NOTE not in content, "freshly indexed .md project reported stale")

        _write(project / "drafts" / "skip-2.md", "# Another draft\n")
        content = _run_prefill(project)
        expect(STALE_NOTE not in content, "skipped drafts/ file made INDEX stale")

        _write(project / "notes" / "b.md", "# B\n")
        content = _run_prefill(project)
        expect(STALE_NOTE not in content, "new .md source left a note")
        expect("notes/b.md" in (project / ps.INDEX_NAME).read_text(encoding="utf-8"),
               "new .md source not added to INDEX.md")

        _write(project / "notes" / "a.md", "# A changed\n")
        content = _run_prefill(project)
        expect(STALE_NOTE not in content, "edited .md source left a note")
        expect(ps.catalog_is_fresh(project), "edited .md source not refreshed")


def test_extra_root_not_checked() -> None:
    """hermes-webui does not mount extra source roots; only local rows count."""
    with tempfile.TemporaryDirectory() as tmp:
        extra = Path(tmp) / "extra-notes"
        _write(extra / "outside.md", "# Outside\n")
        project = Path(tmp) / "with-extra"
        _write(project / "AGENTS.md", BRIEF)
        _write(project / "local.md", "# Local\n")
        _write(
            project / "sources.yaml",
            "version: 1\n"
            "sources:\n"
            "  - id: project\n"
            "    path: .\n"
            "    retrieval: files\n"
            "  - id: extra\n"
            f"    path: {extra}\n"
            "    retrieval: files\n",
        )
        ps.write_index(project)
        expect(
            ps.catalog_is_fresh(project),
            "full catalog should be fresh right after indexing",
        )
        shutil.rmtree(extra)
        content = _run_prefill(project)
        expect(STALE_NOTE not in content, "missing extra root made INDEX stale")

        manifest = (project / ps.MANIFEST_NAME).read_text(encoding="utf-8")
        _write(project / "local.md", "# Local changed\n")
        content = _run_prefill(project)
        expect(STALE_NOTE in content, "edited local source not reported stale")
        expect("refreshes when the stack next starts" in content, content)
        expect((project / ps.MANIFEST_NAME).read_text(encoding="utf-8") == manifest,
               "prefill rewrote an index whose extra root it can't see")


def test_hand_written_index_untouched() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "hand"
        _write(project / "AGENTS.md", BRIEF)
        _write(project / "notes" / "a.md", "# A\n")
        _write(project / ps.INDEX_NAME, "# My index\n")
        content = _run_prefill(project)
        expect(STALE_NOTE not in content, "hand-written index got a note")
        expect((project / ps.INDEX_NAME).read_text(encoding="utf-8") == "# My index\n",
               "prefill changed a hand-written index")
        expect(not (project / ps.MANIFEST_NAME).exists(), "prefill adopted a hand-written index")


def test_read_only_project_falls_back_to_note() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "ro"
        _write(project / "AGENTS.md", BRIEF)
        _write(project / "notes" / "a.md", "# A\n")
        ps.write_index(project)
        _write(project / "notes" / "a.md", "# A changed\n")
        project.chmod(0o555)
        try:
            content = _run_prefill(project)
        finally:
            project.chmod(0o755)
        expect(STALE_NOTE in content, "failed refresh gave no note")
        expect(not ps.catalog_is_fresh(project), "read-only project was rewritten")


def test_container_layout_imports_module() -> None:
    """Compose mounts both files side by side under /bootstrap."""
    with tempfile.TemporaryDirectory() as tmp:
        bootstrap = Path(tmp) / "bootstrap"
        bootstrap.mkdir()
        script = bootstrap / "project-brief-prefill.py"
        shutil.copy2(PREFILL, script)
        shutil.copy2(LIB / "project_sources.py", bootstrap / "project_sources.py")
        project = _txt_and_hidden_project(Path(tmp))
        content = _run_prefill(project, script=script)
        expect(STALE_NOTE not in content, "bootstrap layout reported stale")


def main() -> int:
    tests = [
        test_txt_and_hidden_sources_are_fresh,
        test_markdown_only_project_unchanged,
        test_extra_root_not_checked,
        test_hand_written_index_untouched,
        test_read_only_project_falls_back_to_note,
        test_container_layout_imports_module,
    ]
    for fn in tests:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"{len(tests)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
