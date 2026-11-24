#!/usr/bin/env python3
"""Project source registry: sources.yaml parse, walk, INDEX paths.

Host stdlib only — do not require PyYAML. Used by project-index.py, the
project brief prefill and project_doctor.py (doctor's checks).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

SOURCES_NAME = "sources.yaml"
INDEX_NAME = "INDEX.md"
MANIFEST_NAME = ".index-manifest.json"
MANIFEST_VERSION = 2
INDEX_CAP_PER_SOURCE = 200

SKIP_NAMES = frozenset(
    {
        "AGENTS.md",
        "INDEX.md",
        "README.md",
        SOURCES_NAME,
        MANIFEST_NAME,
    }
)
SKIP_DIR_NAMES = frozenset(
    {"templates", "applications", "artifacts", "drafts", "node_modules"}
)
DEFAULT_INCLUDE = ("**/*.md", "**/*.txt")
IMPLEMENTED_RETRIEVAL = "files"
RESERVED_RETRIEVAL = frozenset({"lightrag", "both"})
ALLOWED_RETRIEVAL = frozenset({IMPLEMENTED_RETRIEVAL}) | RESERVED_RETRIEVAL
SOURCE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")
FRONTMATTER_DESC = re.compile(
    r"(?ms)\A---\s*\n.*?^description:\s*(.+?)\s*$.*?\n---\s*\n"
)
ATX_HEADING = re.compile(r"(?m)^#\s+(.+)$")


class SourcesError(ValueError):
    """Invalid sources.yaml or unsupported retrieval mode."""


@dataclass(frozen=True)
class Source:
    id: str
    path: str
    retrieval: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]


@dataclass(frozen=True)
class IndexedFile:
    path: str
    description: str
    source_id: str
    size: int
    mtime_ns: int
    sha256: str

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "source_id": self.source_id,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
        }


def strip_frontmatter(content: str) -> str:
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


def description_for(path: Path, content: str) -> str:
    match = FRONTMATTER_DESC.match(content.lstrip("\ufeff"))
    if match:
        desc = match.group(1).strip().strip("\"'")
        if desc:
            return desc
    body = strip_frontmatter(content)
    heading = ATX_HEADING.search(body)
    if heading:
        return heading.group(1).strip()
    return path.stem.replace("-", " ").replace("_", " ")


def _parse_flow_list(raw: str) -> list[str] | None:
    text = raw.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return None
    inner = text[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    buf: list[str] = []
    quote = ""
    for ch in inner:
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf.append(ch)
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == ",":
            item = "".join(buf).strip()
            if item:
                items.append(item)
            buf = []
            continue
        buf.append(ch)
    item = "".join(buf).strip()
    if item:
        items.append(item)
    return items


def _parse_scalar(raw: str) -> Any:
    text = raw.strip()
    flow = _parse_flow_list(text)
    if flow is not None:
        return flow
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    if text in ("null", "~", ""):
        return None
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        return text[1:-1]
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _strip_comment(s: str) -> str:
    in_sq = in_dq = False
    out: list[str] = []
    for ch in s:
        if ch == "'" and not in_dq:
            in_sq = not in_sq
        elif ch == '"' and not in_sq:
            in_dq = not in_dq
        elif ch == "#" and not in_sq and not in_dq:
            break
        out.append(ch)
    return "".join(out).rstrip()


def _indent_of(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def _parse_block_list(
    lines: list[str], start: int, base_indent: int
) -> tuple[list[Any], int]:
    items: list[Any] = []
    i = start
    n = len(lines)
    while i < n:
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        line = _strip_comment(raw)
        ind = _indent_of(line)
        if ind < base_indent:
            break
        stripped = line.lstrip()
        if not stripped.startswith("- "):
            break
        item_indent = ind
        body = stripped[2:]
        if ":" in body and not body.lstrip().startswith("http"):
            obj: dict[str, Any] = {}
            key, _, rest = body.partition(":")
            obj[key.strip()] = _parse_scalar(rest)
            i += 1
            while i < n:
                raw2 = lines[i]
                if not raw2.strip() or raw2.lstrip().startswith("#"):
                    i += 1
                    continue
                line2 = _strip_comment(raw2)
                ind2 = _indent_of(line2)
                if ind2 <= item_indent:
                    break
                if line2.lstrip().startswith("- "):
                    nested, i = _parse_block_list(lines, i, base_indent=ind2)
                    # Last key gets the nested list when the value was empty.
                    last = next(reversed(obj))
                    if obj[last] in (None, ""):
                        obj[last] = nested
                    else:
                        break
                    continue
                if ":" not in line2:
                    break
                k2, _, v2 = line2.lstrip().partition(":")
                obj[k2.strip()] = _parse_scalar(v2)
                i += 1
            items.append(obj)
            continue
        items.append(_parse_scalar(body))
        i += 1
    return items, i


def parse_sources_yaml(text: str, *, force_simple: bool = False) -> dict[str, Any]:
    """Parse the constrained sources.yaml subset (or full YAML if PyYAML is present)."""
    if not force_simple:
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(text) or {}
            if isinstance(data, dict):
                return data
        except ImportError:
            pass

    lines = text.splitlines()
    root: dict[str, Any] = {}
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        line = _strip_comment(raw)
        if not line.strip():
            i += 1
            continue
        if _indent_of(line) != 0:
            raise SourcesError(f"unexpected indent at line {i + 1}: {raw}")
        if ":" not in line:
            raise SourcesError(f"expected key: at line {i + 1}")
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest == "":
            j = i + 1
            while j < n and (not lines[j].strip() or lines[j].lstrip().startswith("#")):
                j += 1
            if j < n and lines[j].lstrip().startswith("- "):
                items, i = _parse_block_list(lines, j, base_indent=0)
                root[key] = items
                continue
            root[key] = None
            i += 1
            continue
        root[key] = _parse_scalar(rest)
        i += 1
    return root


def default_sources() -> list[Source]:
    return [
        Source(
            id="project",
            path=".",
            retrieval=IMPLEMENTED_RETRIEVAL,
            include=DEFAULT_INCLUDE,
            exclude=(),
        )
    ]


def _as_str_tuple(value: Any, field: str, source_id: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        flow = _parse_flow_list(value)
        if flow is not None:
            return tuple(flow)
        return (value,)
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise SourcesError(
                    f"source {source_id}: {field} entries must be non-empty strings"
                )
            out.append(item.strip())
        return tuple(out)
    raise SourcesError(f"source {source_id}: {field} must be a list of strings")


def sources_from_data(data: dict[str, Any]) -> list[Source]:
    if not data:
        return default_sources()
    version = data.get("version", 1)
    if version != 1:
        raise SourcesError(f"unsupported sources.yaml version: {version}")
    raw_sources = data.get("sources")
    if raw_sources is None:
        return default_sources()
    if not isinstance(raw_sources, list):
        raise SourcesError("sources.yaml 'sources' must be a list")
    if not raw_sources:
        return default_sources()

    seen: set[str] = set()
    out: list[Source] = []
    for idx, item in enumerate(raw_sources):
        if not isinstance(item, dict):
            raise SourcesError(f"sources[{idx}] must be a mapping")
        sid = str(item.get("id") or "").strip()
        if not sid:
            raise SourcesError(f"sources[{idx}] is missing id")
        if not SOURCE_ID_RE.match(sid):
            raise SourcesError(
                f"invalid source id {sid!r} (use letters, numbers, hyphen, underscore)"
            )
        if sid in seen:
            raise SourcesError(f"duplicate source id: {sid}")
        seen.add(sid)
        path = item.get("path")
        if path is None or not str(path).strip():
            raise SourcesError(f"source {sid}: path is required")
        retrieval = str(item.get("retrieval") or IMPLEMENTED_RETRIEVAL).strip()
        if retrieval not in ALLOWED_RETRIEVAL:
            raise SourcesError(
                f"unknown retrieval {retrieval!r} (source id={sid})"
            )
        if retrieval in RESERVED_RETRIEVAL:
            raise SourcesError(
                f"retrieval {retrieval!r} is not implemented yet (source id={sid})"
            )
        include = _as_str_tuple(item.get("include"), "include", sid) or DEFAULT_INCLUDE
        exclude = _as_str_tuple(item.get("exclude"), "exclude", sid)
        out.append(
            Source(
                id=sid,
                path=str(path).strip(),
                retrieval=retrieval,
                include=include,
                exclude=exclude,
            )
        )
    return out


def load_sources(project_root: Path) -> tuple[list[Source], bool]:
    """Return (sources, explicit). explicit is False when sources.yaml is missing."""
    path = project_root / SOURCES_NAME
    if not path.is_file():
        return default_sources(), False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourcesError(f"cannot read {path}: {exc}") from exc
    data = parse_sources_yaml(text)
    if not isinstance(data, dict):
        raise SourcesError("sources.yaml must be a mapping")
    return sources_from_data(data), True


def _source_root_path(project_root: Path, spec: Source) -> Path:
    raw = spec.path
    if raw.startswith("~/"):
        return Path(raw).expanduser()
    if raw == "~":
        raise SourcesError(f"source {spec.id}: path must not be $HOME alone")
    if raw == "." or not raw.startswith("/"):
        return (project_root / raw).resolve()
    return Path(raw).expanduser()


def is_project_local(project_root: Path, spec: Source) -> bool:
    """True when the source is a relative path that stays inside the project.

    Absolute and ~ paths are extra host roots, even if they happen to point
    into the project, because containers mount the project elsewhere.
    """
    raw = spec.path
    if raw.startswith(("/", "~")):
        return False
    root = project_root.resolve()
    try:
        (root / raw).resolve().relative_to(root)
    except ValueError:
        return False
    return True


def resolve_source_root(project_root: Path, spec: Source) -> Path:
    root = _source_root_path(project_root, spec)
    if not root.is_dir():
        raise SourcesError(f"source {spec.id}: path is not a directory: {root}")
    return root.resolve()


def _glob_match(rel: str, pattern: str) -> bool:
    rel_n = rel.lstrip("./")
    pat = pattern.lstrip("./")
    if fnmatch.fnmatch(rel_n, pat):
        return True
    parts = rel_n.split("/")
    for i in range(len(parts)):
        suffix = "/".join(parts[i:])
        if fnmatch.fnmatch(suffix, pat):
            return True
        if pat.startswith("**/") and fnmatch.fnmatch(suffix, pat[3:]):
            return True
    return False


def _should_skip_rel(rel: str) -> bool:
    parts = Path(rel).parts
    if not parts:
        return True
    name = parts[-1]
    if name in SKIP_NAMES:
        return True
    for part in parts[:-1]:
        if part in SKIP_DIR_NAMES or part.startswith("."):
            return True
    return name.startswith(".")


def is_stack_checkout(folder: Path) -> bool:
    """True when folder is a checkout of this stack with its own state: a
    docker-compose.yml beside data/hermes. Its data/ holds Hermes sessions,
    cron output, logs and the mail and message indexes, which the terminal
    backend already hides from the agent; an INDEX row would leak each file's
    heading. Same marker as doctor's coding-root check."""
    return (folder / "docker-compose.yml").is_file() and (folder / "data" / "hermes").is_dir()


def iter_source_files(source_root: Path, spec: Source) -> list[Path]:
    files: list[Path] = []
    if not source_root.is_dir():
        return files
    # Walk without descending into folders _should_skip_rel would skip anyway
    # (hidden ones such as .git, and SKIP_DIR_NAMES): an extra root can be a
    # whole repository, and walking its .git and worktrees took ~30 s. Like
    # rglob, this doesn't follow symlinked folders.
    candidates: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(source_root):
        base = Path(dirpath)
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".")
            and d not in SKIP_DIR_NAMES
            and not (d == "data" and is_stack_checkout(base))
        ]
        candidates.extend(base / name for name in filenames)
    for path in sorted(candidates):
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        rel = path.relative_to(source_root).as_posix()
        if _should_skip_rel(rel):
            continue
        if spec.exclude and any(_glob_match(rel, pat) for pat in spec.exclude):
            continue
        if not any(_glob_match(rel, pat) for pat in spec.include):
            continue
        files.append(path)
    return files


def index_path_for(file_path: Path, project_root: Path) -> str:
    """Relative if the file is inside the project; host-absolute otherwise."""
    resolved = file_path.resolve()
    root = project_root.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return str(resolved)


def extra_host_roots(project_root: Path, sources: Sequence[Source] | None = None) -> list[Path]:
    """Source directories that are not inside the project tree."""
    if sources is None:
        sources, _ = load_sources(project_root)
    root = project_root.resolve()
    extras: list[Path] = []
    seen: set[Path] = set()
    for spec in sources:
        resolved = resolve_source_root(root, spec)
        try:
            resolved.relative_to(root)
            continue
        except ValueError:
            pass
        if resolved not in seen:
            extras.append(resolved)
            seen.add(resolved)
    return extras


def build_catalog(
    project_root: Path,
    *,
    cap: int = INDEX_CAP_PER_SOURCE,
    local_only: bool = False,
) -> tuple[list[IndexedFile], list[str], bool]:
    """Walk sources and return (files, warnings, grouped).

    grouped is True when sources.yaml was present (INDEX should use source headings).
    local_only skips sources outside the project (see is_project_local).
    """
    root = project_root.resolve()
    sources, explicit = load_sources(root)
    files: list[IndexedFile] = []
    warnings: list[str] = []
    for spec in sources:
        if local_only and not is_project_local(root, spec):
            continue
        source_root = resolve_source_root(root, spec)
        found = iter_source_files(source_root, spec)
        if cap and len(found) > cap:
            warnings.append(
                f"source {spec.id}: {len(found)} files, indexing first {cap} "
                f"(raise the volume or graduate this tree to LightRAG)"
            )
            found = found[:cap]
        for path in found:
            try:
                content = path.read_text(encoding="utf-8")
                data = path.read_bytes()
                st = path.stat()
            except OSError as exc:
                warnings.append(f"source {spec.id}: skipped {path}: {exc}")
                continue
            files.append(
                IndexedFile(
                    path=index_path_for(path, root),
                    description=description_for(path, content),
                    source_id=spec.id,
                    size=st.st_size,
                    mtime_ns=st.st_mtime_ns,
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            )
    return files, warnings, explicit


def render_index(files: Sequence[IndexedFile], *, grouped: bool) -> str:
    lines = [
        "# Index",
        "",
        "Generated by `make project-index`. Do not hand-edit the tables; add a",
        "`description:` field in each source file's YAML frontmatter instead.",
        "",
    ]
    if not files:
        lines.extend(
            [
                "| Path | Description |",
                "| --- | --- |",
                "| _(no source markdown)_ | Add notes under this project, then re-run `make project-index`. |",
                "",
            ]
        )
        return "\n".join(lines)

    if not grouped:
        lines.extend(["| Path | Description |", "| --- | --- |"])
        for item in files:
            safe = item.description.replace("|", "\\|")
            lines.append(f"| `{item.path}` | {safe} |")
        lines.append("")
        return "\n".join(lines)

    by_source: dict[str, list[IndexedFile]] = {}
    order: list[str] = []
    for item in files:
        if item.source_id not in by_source:
            by_source[item.source_id] = []
            order.append(item.source_id)
        by_source[item.source_id].append(item)

    for sid in order:
        rows = by_source[sid]
        lines.append(f"## {sid}")
        lines.append("")
        lines.extend(["| Path | Description |", "| --- | --- |"])
        for item in rows:
            safe = item.description.replace("|", "\\|")
            lines.append(f"| `{item.path}` | {safe} |")
        lines.append("")
    return "\n".join(lines)


def _write_atomic(path: Path, text: str) -> None:
    """Write through a temp file beside path, then rename over it, so a reader
    or a concurrent refresh never sees a half-written file."""
    # mkstemp, not a pid: python3 is pid 1 in every throwaway container, so
    # two writers could share a pid-named temp file.
    fd, raw = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.tmp-")
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        # Keep the old file's mode; mkstemp creates 0600.
        os.chmod(tmp, path.stat().st_mode & 0o7777 if path.exists() else 0o644)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_index(project_root: Path, *, cap: int = INDEX_CAP_PER_SOURCE) -> list[str]:
    """Rewrite INDEX.md and the manifest. The manifest goes last: a failure
    between the two leaves the index stale, and the next refresh redoes it."""
    files, warnings, grouped = build_catalog(project_root, cap=cap)
    index_text = render_index(files, grouped=grouped)
    _write_atomic(project_root / INDEX_NAME, index_text)
    manifest = {
        "version": MANIFEST_VERSION,
        "files": [item.manifest_entry() for item in files],
    }
    _write_atomic(
        project_root / MANIFEST_NAME,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    return warnings


GENERATED = "generated"
HAND_WRITTEN = "hand-written"
NO_INDEX = "none"


def index_kind(project_root: Path) -> str:
    """GENERATED when the manifest exists (the indexer wrote INDEX.md and owns
    it) or when sources.yaml asks for an index that hasn't been written yet,
    HAND_WRITTEN when INDEX.md exists without a manifest (the user's file,
    never overwritten or checked), NO_INDEX otherwise. The placeholder INDEX.md
    that make project-init copied before it indexed new projects counts as
    GENERATED: nobody wrote it by hand."""
    if (project_root / MANIFEST_NAME).is_file():
        return GENERATED
    index = project_root / INDEX_NAME
    if index.is_file():
        return GENERATED if _is_old_template_index(index) else HAND_WRITTEN
    if (project_root / SOURCES_NAME).is_file():
        return GENERATED
    return NO_INDEX


# The second line of every placeholder INDEX.md the project template shipped.
_OLD_TEMPLATE_LINES = (
    "Run from the repo root after you add or change source files or `sources.yaml`:",
    "Run from the repo root after you add or change source markdown:",
)


def _is_old_template_index(index: Path) -> bool:
    try:
        lines = index.read_text(encoding="utf-8").splitlines()[:3]
    except (OSError, ValueError):
        return False
    return len(lines) == 3 and lines[0] == "# Index" and lines[2] in _OLD_TEMPLATE_LINES


def all_sources_local(project_root: Path) -> bool:
    """True when every source sits inside the project, so a container that
    mounts only the project can rewrite the whole index."""
    sources, _ = load_sources(project_root.resolve())
    return all(is_project_local(project_root, spec) for spec in sources)


def _is_local_row(item: Any, local_ids: set[str]) -> bool:
    if not isinstance(item, dict):
        return True  # malformed; let the comparison report it stale
    if "source_id" in item:
        return item["source_id"] in local_ids
    # Version 1 manifests have no source_id; extra roots used absolute paths.
    return not str(item.get("path") or "").startswith("/")


def catalog_is_fresh(
    project_root: Path,
    *,
    cap: int = INDEX_CAP_PER_SOURCE,
    local_only: bool = False,
) -> bool:
    """Compare the manifest with a fresh walk of the sources.

    local_only checks only sources inside the project and ignores manifest rows
    from the others, for callers that cannot see extra host roots
    (hermes-webui's project-brief-prefill.py).
    """
    manifest_path = project_root / MANIFEST_NAME
    if not manifest_path.is_file() or not (project_root / INDEX_NAME).is_file():
        return False
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    recorded = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(recorded, list):
        return False
    if local_only:
        sources, _ = load_sources(project_root.resolve())
        local_ids = {
            spec.id for spec in sources if is_project_local(project_root, spec)
        }
        recorded = [item for item in recorded if _is_local_row(item, local_ids)]
    current, _, _ = build_catalog(project_root, cap=cap, local_only=local_only)
    if len(recorded) != len(current):
        return False
    by_path = {item.path: item for item in current}
    for item in recorded:
        if not isinstance(item, dict):
            return False
        key = item.get("path")
        if key not in by_path:
            return False
        cur = by_path[key]
        if (
            item.get("size") != cur.size
            or item.get("mtime_ns") != cur.mtime_ns
            or item.get("sha256") != cur.sha256
        ):
            return False
    return True


def survey_projects(projects_root: Path, *, refresh: bool) -> dict[str, list]:
    """Check every project folder's index; with refresh, rewrite stale
    generated ones. Returns project names under fresh, refreshed (or stale
    without refresh) and hand_written, and {project, error} under failed.
    Folders without an index are left out."""
    changed = "refreshed" if refresh else "stale"
    result: dict[str, list] = {"fresh": [], changed: [], "hand_written": [], "failed": []}
    for child in sorted(projects_root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        kind = index_kind(child)
        if kind == HAND_WRITTEN:
            result["hand_written"].append(child.name)
            continue
        if kind != GENERATED:
            continue
        try:
            if catalog_is_fresh(child):
                result["fresh"].append(child.name)
            else:
                if refresh:
                    write_index(child)
                result[changed].append(child.name)
        except (SourcesError, OSError, ValueError) as exc:  # ValueError: a non-UTF-8 source
            result["failed"].append({"project": child.name, "error": str(exc)})
    return result
