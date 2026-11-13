"""Reduce an Accessibility.getFullAXTree result to what the agent may see.

Keeps the role and accessible name of each node that is not ignored, plus the
text of StaticText/text nodes. Never keeps a node's ``value``. A textbox,
searchbox or combobox whose name is derived from its value (nameFrom
"contents" or "value", or a name equal to its value) has its name dropped,
because a filled field would otherwise echo the typed text through its name.
Refs are numbered from 1 in tree order and map to backendDOMNodeId.
"""

from __future__ import annotations

_FIELD_ROLES = {"textbox", "searchbox", "combobox"}
_VALUE_NAME_FROM = {"contents", "value"}
_TEXT_ROLES = {"StaticText", "text", "InlineTextBox"}


def _val(obj) -> str:
    if isinstance(obj, dict):
        v = obj.get("value")
        return v if isinstance(v, str) else ("" if v is None else str(v))
    return ""


def _prop(node: dict, name: str):
    for p in node.get("properties") or ():
        if isinstance(p, dict) and p.get("name") == name:
            return _val(p.get("value"))
    return None


def reduce(ax_nodes: list[dict]) -> tuple[list[dict], dict[int, int]]:
    items: list[dict] = []
    refs: dict[int, int] = {}
    for node in ax_nodes:
        if not isinstance(node, dict) or node.get("ignored"):
            continue
        role = _val(node.get("role"))
        name = _val(node.get("name"))
        if role in _FIELD_ROLES:
            value = _val(node.get("value")) or _prop(node, "value") or ""
            derived = _prop(node, "nameFrom") in _VALUE_NAME_FROM or (value != "" and name == value)
            if derived:
                name = ""
        text = name if role in _TEXT_ROLES else ""
        if role in _TEXT_ROLES:
            name = ""
        ref = len(items) + 1
        items.append({"ref": ref, "role": role, "name": name, "text": text})
        backend = node.get("backendDOMNodeId")
        if isinstance(backend, int):
            refs[ref] = backend
    return items, refs
