#!/usr/bin/env python3
"""Minimal loader for eval/tool-calling/cases.yaml (no PyYAML required).

Supports the subset this repo uses: top-level mapping, list of mappings,
plain scalars, `|` block scalars, nested string lists, and list-of-one-key maps.
Prefers PyYAML when installed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence


def session_outcome(
    names: Iterable[str] | None,
    outcomes: Sequence[str] | None,
    *,
    recovered: bool = False,
    has_last_run: bool = False,
) -> str:
    """Roll per-turn tracer outcomes into one session outcome.

    A final prose answer after tools is still tools_dispatched. text_only
    only when no tool_calls ran at all (bridge tools still count as tools).
    """
    called = [str(n) for n in (names or []) if n]
    turns = list(outcomes or [])
    if recovered or any(o == "recovered_then_dispatched" for o in turns):
        return "recovered_then_dispatched"
    if called or any(o == "tools_dispatched" for o in turns):
        return "tools_dispatched"
    if turns:
        return "text_only"
    return "api_only" if has_last_run else "no_trace"


def load_cases(path: str | Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        if isinstance(data, dict):
            return data
    except ImportError:
        pass
    return _parse_simple(text)


def _parse_simple(text: str) -> dict:
    lines = text.splitlines()
    root: dict[str, Any] = {}
    i = 0
    n = len(lines)

    def indent_of(s: str) -> int:
        return len(s) - len(s.lstrip(" "))

    def strip_comment(s: str) -> str:
        in_sq = in_dq = False
        out = []
        for ch in s:
            if ch == "'" and not in_dq:
                in_sq = not in_sq
            elif ch == '"' and not in_sq:
                in_dq = not in_dq
            elif ch == "#" and not in_sq and not in_dq:
                break
            out.append(ch)
        return "".join(out).rstrip()

    def parse_scalar(raw: str) -> Any:
        raw = raw.strip()
        if raw in ("true", "True"):
            return True
        if raw in ("false", "False"):
            return False
        if raw in ("null", "~", ""):
            return None
        if (raw.startswith('"') and raw.endswith('"')) or (
            raw.startswith("'") and raw.endswith("'")
        ):
            return raw[1:-1]
        return raw

    while i < n:
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        line = strip_comment(raw)
        if not line.strip():
            i += 1
            continue
        ind = indent_of(line)
        if ind != 0:
            raise ValueError(f"unexpected indent at line {i + 1}: {raw}")
        if ":" not in line:
            raise ValueError(f"expected key: at line {i + 1}")
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest == "" or rest == "|":
            # Look ahead: list or block
            j = i + 1
            while j < n and (not lines[j].strip() or lines[j].lstrip().startswith("#")):
                j += 1
            if j < n and lines[j].lstrip().startswith("- "):
                items, i = _parse_list(lines, j, base_indent=0)
                root[key] = items
                continue
            if rest == "|":
                block, i = _parse_block(lines, i + 1, parent_indent=0)
                root[key] = block
                continue
            root[key] = None
            i += 1
            continue
        root[key] = parse_scalar(rest)
        i += 1
    return root


def _parse_block(lines: list[str], start: int, parent_indent: int) -> tuple[str, int]:
    parts: list[str] = []
    i = start
    block_indent = None
    while i < len(lines):
        raw = lines[i]
        if not raw.strip():
            parts.append("")
            i += 1
            continue
        ind = len(raw) - len(raw.lstrip(" "))
        if ind <= parent_indent:
            break
        if block_indent is None:
            block_indent = ind
        parts.append(raw[block_indent:])
        i += 1
    # Trim trailing empty lines; keep internal newlines.
    while parts and parts[-1] == "":
        parts.pop()
    return ("\n".join(parts) + ("\n" if parts else "")), i


def _parse_list(lines: list[str], start: int, base_indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    i = start
    n = len(lines)

    def indent_of(s: str) -> int:
        return len(s) - len(s.lstrip(" "))

    def strip_comment(s: str) -> str:
        if "#" in s:
            # Good enough for our files (no # in values).
            return s.split("#", 1)[0].rstrip()
        return s.rstrip()

    def parse_scalar(raw: str) -> Any:
        raw = raw.strip()
        if raw in ("true", "True"):
            return True
        if raw in ("false", "False"):
            return False
        if (raw.startswith('"') and raw.endswith('"')) or (
            raw.startswith("'") and raw.endswith("'")
        ):
            return raw[1:-1]
        return raw

    while i < n:
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        line = strip_comment(raw)
        ind = indent_of(line)
        if ind < base_indent + 2 and not line.lstrip().startswith("- "):
            break
        if ind < base_indent:
            break
        stripped = line.lstrip()
        if not stripped.startswith("- "):
            break
        item_indent = ind
        body = stripped[2:]
        if body == "" or body == "|":
            # nested structure under list item
            j = i + 1
            while j < n and (not lines[j].strip() or lines[j].lstrip().startswith("#")):
                j += 1
            if j < n and lines[j].lstrip().startswith("- "):
                nested, i = _parse_list(lines, j, base_indent=item_indent)
                items.append(nested)
                continue
            items.append(None)
            i += 1
            continue
        if ":" in body and not body.startswith("http"):
            # mapping entry: "- id: web-fact" starts a dict
            obj: dict[str, Any] = {}
            k, _, v = body.partition(":")
            k = k.strip()
            v = v.strip()
            if v == "|":
                block, i = _parse_block(lines, i + 1, parent_indent=item_indent)
                obj[k] = block
            elif v == "":
                # nested list or map on following lines
                j = i + 1
                while j < n and (not lines[j].strip() or lines[j].lstrip().startswith("#")):
                    j += 1
                if j < n and lines[j].lstrip().startswith("- "):
                    nested, i = _parse_list(lines, j, base_indent=item_indent)
                    obj[k] = nested
                else:
                    obj[k] = None
                    i += 1
            else:
                obj[k] = parse_scalar(v)
                i += 1
            # Continue reading siblings of this mapping at greater indent
            while i < n:
                raw2 = lines[i]
                if not raw2.strip() or raw2.lstrip().startswith("#"):
                    i += 1
                    continue
                line2 = strip_comment(raw2)
                ind2 = indent_of(line2)
                if ind2 <= item_indent:
                    break
                if line2.lstrip().startswith("- "):
                    break
                if ":" not in line2:
                    break
                k2, _, v2 = line2.lstrip().partition(":")
                k2 = k2.strip()
                v2 = v2.strip()
                if v2 == "|":
                    block, i = _parse_block(lines, i + 1, parent_indent=ind2)
                    obj[k2] = block
                elif v2 == "":
                    j = i + 1
                    while j < n and (
                        not lines[j].strip() or lines[j].lstrip().startswith("#")
                    ):
                        j += 1
                    if j < n and lines[j].lstrip().startswith("- "):
                        nested, i = _parse_list(lines, j, base_indent=ind2)
                        obj[k2] = nested
                    else:
                        obj[k2] = None
                        i += 1
                else:
                    obj[k2] = parse_scalar(v2)
                    i += 1
            items.append(obj)
            continue
        # plain scalar list item
        items.append(parse_scalar(body))
        i += 1
    return items, i


if __name__ == "__main__":
    import json
    import sys

    print(json.dumps(load_cases(sys.argv[1]), indent=2))
