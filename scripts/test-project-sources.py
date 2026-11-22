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

from testkit import expect, fail  # noqa: E402


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


def test_extra_root_skips_stack_data_and_dependencies() -> None:
    """A source that is a whole stack checkout never indexes its data/ (Hermes
    state the terminal backend hides), and no source indexes node_modules."""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project = tmp / "proj"
        stack = tmp / "stack"
        project.mkdir()
        for rel in ("docs/guide.md", "data/hermes/cron/output/run.md",
                    "data/notes.md", "node_modules/pkg/README.txt", "web/node_modules/x/a.md"):
            path = stack / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {rel}\n", encoding="utf-8")
        (stack / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        plain = tmp / "plain"
        (plain / "data").mkdir(parents=True)
        (plain / "data" / "kept.md").write_text("# Kept\n", encoding="utf-8")
        (project / "sources.yaml").write_text(
            "version: 1\nsources:\n"
            f"  - id: stack\n    path: {stack}\n    retrieval: files\n"
            f"  - id: plain\n    path: {plain}\n    retrieval: files\n",
            encoding="utf-8",
        )
        paths = sorted(item.path for item in ps.build_catalog(project)[0])
        expect(paths == [str((plain / "data" / "kept.md").resolve()),
                         str((stack / "docs" / "guide.md").resolve())], paths)


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


def _run_indexer(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(INDEXER), *args],
        check=False,
        capture_output=True,
        text=True,
    )


HAND_WRITTEN = "# My own index\n\nKept by hand.\n"


def _projects_tree(base: Path) -> Path:
    """projects/ with one fresh, one stale and one hand-written project, one
    project with no index, and a hidden folder."""
    projects = base / "projects"
    projects.mkdir()
    for name in ("fresh", "stale"):
        shutil.copytree(FIXTURE, projects / name)
        ps.write_index(projects / name)
    (projects / "stale" / "notes" / "new.md").write_text("# Added later\n", encoding="utf-8")
    hand = projects / "hand"
    shutil.copytree(FIXTURE, hand)
    (hand / ps.MANIFEST_NAME).unlink()
    (hand / ps.INDEX_NAME).write_text(HAND_WRITTEN, encoding="utf-8")
    (projects / "bare").mkdir()
    (projects / ".hidden").mkdir()
    return projects


def test_index_kind() -> None:
    with tempfile.TemporaryDirectory() as raw:
        projects = _projects_tree(Path(raw))
        expect(ps.index_kind(projects / "fresh") == ps.GENERATED, "fresh")
        expect(ps.index_kind(projects / "hand") == ps.HAND_WRITTEN, "hand")
        expect(ps.index_kind(projects / "bare") == ps.NO_INDEX, "bare")


def test_hand_written_refused_without_force() -> None:
    with tempfile.TemporaryDirectory() as raw:
        hand = _projects_tree(Path(raw)) / "hand"
        proc = _run_indexer("--root", str(hand))
        expect(proc.returncode == 3, f"exit {proc.returncode}: {proc.stderr}")
        expect("--force" in proc.stderr, proc.stderr)
        expect((hand / ps.INDEX_NAME).read_text(encoding="utf-8") == HAND_WRITTEN,
               "refused run changed the hand-written index")
        check = _run_indexer("--root", str(hand), "--check")
        expect(check.returncode == 0, f"--check exit {check.returncode}")
        expect("hand-written (not checked)" in check.stdout, check.stdout)
        forced = _run_indexer("--root", str(hand), "--force")
        expect(forced.returncode == 0, forced.stderr)
        expect(ps.index_kind(hand) == ps.GENERATED, "--force did not adopt the index")
        expect(ps.catalog_is_fresh(hand), "--force left the index stale")


def test_check_all_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as raw:
        projects = _projects_tree(Path(raw))
        before = (projects / "stale" / ps.INDEX_NAME).read_text(encoding="utf-8")
        proc = _run_indexer("--check-all", str(projects), "--json")
        expect(proc.returncode == 0, proc.stderr)
        result = json.loads(proc.stdout)
        expect(result == {"fresh": ["fresh"], "stale": ["stale"],
                          "hand_written": ["hand"], "failed": []}, result)
        expect((projects / "stale" / ps.INDEX_NAME).read_text(encoding="utf-8") == before,
               "--check-all rewrote an index")


def test_refresh_all() -> None:
    with tempfile.TemporaryDirectory() as raw:
        projects = _projects_tree(Path(raw))
        (projects / "broken").mkdir()
        (projects / "broken" / ps.MANIFEST_NAME).write_text("{}\n", encoding="utf-8")
        (projects / "broken" / "sources.yaml").write_text(
            "version: 1\nsources:\n  - id: kb\n    path: .\n    retrieval: magic\n",
            encoding="utf-8",
        )
        proc = _run_indexer("--refresh-all", str(projects), "--json")
        expect(proc.returncode == 0, proc.stderr)
        result = json.loads(proc.stdout)
        expect(result["refreshed"] == ["stale"], result)
        expect(result["fresh"] == ["fresh"], result)
        expect(result["hand_written"] == ["hand"], result)
        expect([f["project"] for f in result["failed"]] == ["broken"], result)
        expect(ps.catalog_is_fresh(projects / "stale"), "stale project still stale")
        expect("notes/new.md" in (projects / "stale" / ps.INDEX_NAME).read_text(encoding="utf-8"),
               "refreshed index misses the new file")
        expect((projects / "hand" / ps.INDEX_NAME).read_text(encoding="utf-8") == HAND_WRITTEN,
               "--refresh-all touched the hand-written index")
        expect(not (projects / "bare" / ps.INDEX_NAME).exists(), "--refresh-all indexed a bare folder")
        leftovers = [p.name for p in projects.rglob(".*.tmp-*")]
        expect(not leftovers, f"temp files left behind: {leftovers}")
        human = _run_indexer("--refresh-all", str(projects))
        expect("0 refreshed, 2 fresh, 1 hand-written, 1 failed" in human.stdout, human.stdout)
        missing = _run_indexer("--refresh-all", str(projects / "nope"))
        expect(missing.returncode == 2, f"missing dir exit {missing.returncode}")


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


def test_template_points_at_context_root_add_recreate() -> None:
    """The project template must not name a service to recreate: with the
    default ssh backend the bind lands on hermes-worker, and context-root.sh
    prints the right command itself (AS-0005)."""
    template = ROOT / "compose" / "hermes" / "project-template"
    for name in ("sources.yaml", "AGENTS.md"):
        text = (template / name).read_text()
        flat = " ".join(text.split())
        expect("--force-recreate hermes" not in flat, f"{name} names the hermes service to recreate")
        expect("Hermes recreate" not in flat, f"{name} says 'a Hermes recreate'")
        expect("recreate command it prints" in flat, f"{name} does not point at context-root-add's recreate command")
    script = (ROOT / "scripts" / "context-root.sh").read_text()
    expect("--force-recreate $(terminal_service)" in script,
           "context-root.sh no longer prints the terminal service recreate the template points at")


def main() -> int:
    tests = [
        test_default_walk_skips_drafts,
        test_render_ungrouped_matches_fixture_table,
        test_extra_root_absolute_paths,
        test_extra_root_skips_stack_data_and_dependencies,
        test_retrieval_lightrag_refused,
        test_retrieval_both_refused,
        test_stdlib_yaml_flow_and_block,
        test_unknown_retrieval_refused,
        test_indexer_cli_roundtrip,
        test_index_kind,
        test_hand_written_refused_without_force,
        test_check_all_writes_nothing,
        test_refresh_all,
        test_visibility_helpers,
        test_template_points_at_context_root_add_recreate,
    ]
    for fn in tests:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"{len(tests)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
