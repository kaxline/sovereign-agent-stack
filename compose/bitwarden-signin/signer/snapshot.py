"""Reduce an Accessibility.getFullAXTree result to what the agent may see.

Keeps the role, accessible name and visible text of nodes that are not ignored,
and never a node's ``value``. Editable fields are handled so typed text cannot
leak:

- A field node (textbox, searchbox, combobox, spinbutton) keeps its name only
  when the winning name source is a label the page author wrote: a
  non-superseded ``name.sources`` entry that has a ``value`` and whose ``type``
  is ``attribute`` (aria-label, title), ``relatedElement`` (aria-labelledby or
  a <label>) or ``placeholder``. A name from ``contents``, from ``value``, or
  with no sources is dropped, because the text of the field would otherwise
  come back through its name.
- Every descendant of a field node, through ``childIds``, is skipped. Chromium
  gives a filled input a generic child holding a StaticText and InlineTextBox
  with the typed value (bullets for a password input).

Text comes from StaticText, text and InlineTextBox nodes outside fields (the
node's name goes in ``text``, ``name`` stays empty). Refs are numbered from 1 in
list order and map to backendDOMNodeId.
"""

from __future__ import annotations

_FIELD_ROLES = {"textbox", "searchbox", "combobox", "spinbutton", "textField", "TextField"}
_LABEL_SOURCE_TYPES = {"attribute", "relatedElement", "placeholder"}
_TEXT_ROLES = {"StaticText", "text", "InlineTextBox"}


def _val(obj) -> str:
    if isinstance(obj, dict):
        v = obj.get("value")
        return v if isinstance(v, str) else ("" if v is None else str(v))
    return ""


def _label_named(node: dict) -> bool:
    """True when the name comes from an author-written label source."""
    name = node.get("name")
    sources = name.get("sources") if isinstance(name, dict) else None
    if not isinstance(sources, list):
        return False
    for src in sources:
        if not isinstance(src, dict) or src.get("superseded") or "value" not in src:
            continue
        return src.get("type") in _LABEL_SOURCE_TYPES
    return False


def _field_descendants(nodes: list[dict]) -> set[str]:
    by_id = {str(n.get("nodeId")): n for n in nodes if isinstance(n, dict)}
    skip: set[str] = set()
    stack = [str(c) for n in nodes if isinstance(n, dict) and _val(n.get("role")) in _FIELD_ROLES
             for c in n.get("childIds") or ()]
    while stack:
        nid = stack.pop()
        if nid in skip:
            continue
        skip.add(nid)
        child = by_id.get(nid)
        if child:
            stack.extend(str(c) for c in child.get("childIds") or ())
    return skip


def reduce(ax_nodes: list[dict]) -> tuple[list[dict], dict[int, int]]:
    skip = _field_descendants(ax_nodes)
    items: list[dict] = []
    refs: dict[int, int] = {}
    for node in ax_nodes:
        if not isinstance(node, dict) or node.get("ignored") or str(node.get("nodeId")) in skip:
            continue
        role = _val(node.get("role"))
        name = _val(node.get("name"))
        if role in _FIELD_ROLES and not _label_named(node):
            name = ""
        text = ""
        if role in _TEXT_ROLES:
            text, name = name, ""
        ref = len(items) + 1
        items.append({"ref": ref, "role": role, "name": name, "text": text})
        backend = node.get("backendDOMNodeId")
        if isinstance(backend, int):
            refs[ref] = backend
    return items, refs
