#!/usr/bin/env python3
"""Stdlib checks for project_sources / project-index (no PyYAML required)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = Path(__file__).resolve().parent / "lib"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample-project"
INDEXER = Path(__file__).resolve().parent / "project-index.py"

if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import project_sources as ps  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def test_default_walk_skips_drafts() -> None:
    files, warnings, grouped = ps.build_catalog(FIXTURE)
    expect(not grouped, "missing sources.yaml should not group INDEX")
    expect(not warnings, f"unexpected warnings: {warnings}")
    paths = [item.path for item in files]
    expect(
        paths == ["notes/heading-only.md", "notes/with-description.md"],
        f"unexpected paths: {paths}",
    )
    by_path = {item.path: item.description for item in files}
    expect(
        by_path["notes/heading-only.md"] == "Beta heading only",
        f"heading description: {by_path['notes/heading-only.md']!r}",
    )
    expect(
        by_path["notes/with-description.md"]
        == "Note with an explicit INDEX description",
        f"frontmatter description: {by_path['notes/with-description.md']!r}",
    )
    expect(
        all(item.source_id == "project" for item in files),
        "implicit source id should be project",
    )


def test_render_ungrouped_matches_fixture_table() -> None:
    files, _, grouped = ps.build_catalog(FIXTURE)
    text = ps.render_index(files, grouped=grouped)
    expect("| `notes/heading-only.md` | Beta heading only |" in text, text)
    expect("## project" not in text, "implicit catalog should stay a flat table")


def test_extra_root_absolute_paths() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project = tmp / "proj"
        extra = tmp / "vault"
        project.mkdir()
        extra.mkdir()
        (project / "notes").mkdir()
        (project / "notes" / "local.md").write_text(
            "---\ndescription: In-project note\n---\n\n# Local\n",
            encoding="utf-8",
        )
        (extra / "remote.md").write_text("# Vault note\n", encoding="utf-8")
        (extra / ".obsidian").mkdir()
        (extra / ".obsidian" / "app.md").write_text("# hidden\n", encoding="utf-8")
        (project / "sources.yaml").write_text(
            "\n".join(
                [
                    "version: 1",
                    "sources:",
                    "  - id: project",
                    "    path: .",
                    "    retrieval: files",
                    '    include: ["**/*.md"]',
                    "  - id: vault",
                    f"    path: {extra}",
                    "    retrieval: files",
                    '    include: ["**/*.md"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        files, warnings, grouped = ps.build_catalog(project)
        expect(grouped, "explicit sources.yaml should group INDEX")
        expect(not warnings, f"unexpected warnings: {warnings}")
        paths = {item.path: item for item in files}
        expect("notes/local.md" in paths, f"missing local: {list(paths)}")
        remote = str((extra / "remote.md").resolve())
        expect(remote in paths, f"missing absolute extra path: {list(paths)}")
        expect(paths[remote].source_id == "vault", paths[remote].source_id)
        expect(
            all(".obsidian" not in p for p in paths),
            f"hidden dir leaked: {list(paths)}",
        )
        extras = [str(p) for p in ps.extra_host_roots(project)]
        expect(extras == [str(extra.resolve())], extras)
        rendered = ps.render_index(files, grouped=True)
        expect("## vault" in rendered and "## project" in rendered, rendered)


def test_retrieval_lightrag_refused() -> None:
    with tempfile.TemporaryDirectory() as raw:
        project = Path(raw)
        (project / "sources.yaml").write_text(
            "version: 1\nsources:\n  - id: kb\n    path: .\n    retrieval: lightrag\n",
            encoding="utf-8",
        )
        try:
            ps.load_sources(project)
        except ps.SourcesError as exc:
            expect("not implemented" in str(exc), str(exc))
        else:
            fail("expected SourcesError for retrieval: lightrag")


def test_stdlib_yaml_flow_and_block() -> None:
    text = "\n".join(
        [
            "version: 1",
            "sources:",
            "  - id: project",
            "    path: .",
            "    retrieval: files",
            '    include: ["**/*.md", "**/*.txt"]',
            "  - id: vault",
            "    path: /tmp/vault",
            "    retrieval: files",
            "    include:",
            '      - "**/*.md"',
            "    exclude:",
            '      - ".obsidian/**"',
            "",
        ]
    )
    data = ps.parse_sources_yaml(text, force_simple=True)
    sources = ps.sources_from_data(data)
    expect(len(sources) == 2, sources)
    expect(sources[0].include == ("**/*.md", "**/*.txt"), sources[0].include)
    expect(sources[1].exclude == (".obsidian/**",), sources[1].exclude)


def test_retrieval_both_refused() -> None:
    with tempfile.TemporaryDirectory() as raw:
        project = Path(raw)
        (project / "sources.yaml").write_text(
            "version: 1\nsources:\n  - id: kb\n    path: .\n    retrieval: both\n",
            encoding="utf-8",
        )
        try:
            ps.load_sources(project)
        except ps.SourcesError as exc:
            expect("not implemented" in str(exc), str(exc))
        else:
            fail("expected SourcesError for retrieval: both")


def test_unknown_retrieval_refused() -> None:
    with tempfile.TemporaryDirectory() as raw:
        project = Path(raw)
        (project / "sources.yaml").write_text(
            "version: 1\nsources:\n  - id: kb\n    path: .\n    retrieval: magic\n",
            encoding="utf-8",
        )
        try:
            ps.load_sources(project)
        except ps.SourcesError as exc:
            expect("unknown retrieval" in str(exc), str(exc))
        else:
            fail("expected SourcesError for unknown retrieval")


def test_indexer_cli_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as raw:
        dest = Path(raw) / "sample"
        shutil.copytree(FIXTURE, dest)
        proc = subprocess.run(
            [sys.executable, str(INDEXER), "--root", str(dest)],
            check=False,
            capture_output=True,
            text=True,
        )
        expect(proc.returncode == 0, proc.stderr or proc.stdout)
        check = subprocess.run(
            [sys.executable, str(INDEXER), "--root", str(dest), "--check"],
            check=False,
            capture_output=True,
            text=True,
        )
        expect(check.returncode == 0, check.stderr or check.stdout)
        manifest = json.loads((dest / ".index-manifest.json").read_text(encoding="utf-8"))
        expect(manifest.get("version") == 2, manifest)
        expect(
            all("source_id" in item for item in manifest["files"]),
            "manifest rows need source_id",
        )


def test_visibility_helpers() -> None:
    prefixes = ps.visibility_prefixes(
        coding_primary="/Users/you/code",
        coding_extra=":/Users/you/other",
        context_extra=":/Users/you/Notes",
    )
    expect("/Users/you/Notes" in prefixes, prefixes)
    # path_is_visible resolves; use prefixes that exist in this run.
    here = ROOT.resolve()
    expect(ps.path_is_visible(here / "scripts", [str(here)]), "repo should be visible")
    expect(
        not ps.path_is_visible(here, ["/tmp/does-not-cover-repo"]),
        "unrelated prefix should not cover",
    )


def main() -> int:
    tests = [
        test_default_walk_skips_drafts,
        test_render_ungrouped_matches_fixture_table,
        test_extra_root_absolute_paths,
        test_retrieval_lightrag_refused,
        test_retrieval_both_refused,
        test_stdlib_yaml_flow_and_block,
        test_unknown_retrieval_refused,
        test_indexer_cli_roundtrip,
        test_visibility_helpers,
    ]
    for fn in tests:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"{len(tests)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
