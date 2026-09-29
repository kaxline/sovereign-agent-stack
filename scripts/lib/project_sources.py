#!/usr/bin/env python3
"""Project source registry: sources.yaml parse, walk, INDEX paths.

Host stdlib only — do not require PyYAML. Used by project-index.py and doctor.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

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
SKIP_DIR_NAMES = frozenset({"templates", "applications", "artifacts", "drafts"})
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


def resolve_source_root(project_root: Path, spec: Source) -> Path:
    raw = spec.path
    if raw.startswith("~/"):
        root = Path(raw).expanduser()
    elif raw == "~":
        raise SourcesError(f"source {spec.id}: path must not be $HOME alone")
    elif raw == "." or not raw.startswith("/"):
        root = (project_root / raw).resolve()
    else:
        root = Path(raw).expanduser()
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


def iter_source_files(source_root: Path, spec: Source) -> list[Path]:
    files: list[Path] = []
    if not source_root.is_dir():
        return files
    for path in sorted(source_root.rglob("*")):
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
) -> tuple[list[IndexedFile], list[str], bool]:
    """Walk sources and return (files, warnings, grouped).

    grouped is True when sources.yaml was present (INDEX should use source headings).
    """
    root = project_root.resolve()
    sources, explicit = load_sources(root)
    files: list[IndexedFile] = []
    warnings: list[str] = []
    for spec in sources:
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


def write_index(project_root: Path, *, cap: int = INDEX_CAP_PER_SOURCE) -> list[str]:
    files, warnings, grouped = build_catalog(project_root, cap=cap)
    index_text = render_index(files, grouped=grouped)
    (project_root / INDEX_NAME).write_text(index_text, encoding="utf-8")
    manifest = {
        "version": MANIFEST_VERSION,
        "files": [item.manifest_entry() for item in files],
    }
    (project_root / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return warnings


def catalog_is_fresh(project_root: Path, *, cap: int = INDEX_CAP_PER_SOURCE) -> bool:
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
    current, _, _ = build_catalog(project_root, cap=cap)
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


def split_colon_paths(raw: str) -> list[str]:
    out: list[str] = []
    for part in (raw or "").split(":"):
        part = part.strip()
        if part:
            out.append(part)
    return out


def path_is_visible(path: str | Path, prefixes: Iterable[str]) -> bool:
    candidate = str(Path(path).expanduser().resolve())
    for prefix in prefixes:
        if not prefix:
            continue
        outer = str(Path(prefix).expanduser().resolve()) if Path(prefix).exists() else prefix.rstrip("/")
        if candidate == outer or candidate.startswith(outer + "/"):
            return True
    return False


def visibility_prefixes(
    *,
    coding_primary: str = "",
    coding_extra: str = "",
    context_extra: str = "",
) -> list[str]:
    prefixes = []
    if coding_primary:
        prefixes.append(coding_primary)
    prefixes.extend(split_colon_paths(coding_extra))
    prefixes.extend(split_colon_paths(context_extra))
    return prefixes


def _doctor_visibility(
    projects_root: Path,
    coding_primary: str,
    coding_extra: str,
    context_extra: str,
) -> int:
    """Print TAB-separated OK/WARN lines for extra source roots."""
    if not projects_root.is_dir():
        return 0
    prefixes = visibility_prefixes(
        coding_primary=coding_primary,
        coding_extra=coding_extra,
        context_extra=context_extra,
    )
    for child in sorted(projects_root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not (child / SOURCES_NAME).is_file():
            continue
        try:
            extras = extra_host_roots(child)
        except SourcesError as exc:
            sys.stdout.write(f"WARN\tsources.yaml ({child.name}): {exc}\n")
            continue
        if not extras:
            sys.stdout.write(
                f"OK\tsources.yaml ({child.name}): no extra host roots\n"
            )
            continue
        for extra in extras:
            shown = str(extra)
            if path_is_visible(extra, prefixes):
                sys.stdout.write(f"OK\tcontext visible ({child.name}): {shown}\n")
            else:
                sys.stdout.write(
                    f"WARN\tsource {child.name} lists {shown} but it is not a "
                    f"context/coding root — run: make context-root-add DIR={shown}\n"
                )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stderr.write(
            "Usage: project_sources.py doctor-visibility "
            "<projects-root> [coding-primary] [coding-extra] [context-extra]\n"
        )
        return 2
    if args[0] == "doctor-visibility":
        if len(args) < 2:
            sys.stderr.write("doctor-visibility requires a projects root\n")
            return 2
        return _doctor_visibility(
            Path(args[1]).expanduser(),
            args[2] if len(args) > 2 else "",
            args[3] if len(args) > 3 else "",
            args[4] if len(args) > 4 else "",
        )
    sys.stderr.write(f"unknown command: {args[0]}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
