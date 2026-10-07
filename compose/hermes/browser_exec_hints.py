"""Hints and small rewrites for browser_exec and browser_vault_* results.

Compatibility overlay for this stack. browser_exec runs `code` with the
browser helpers already defined and returns stdout only. Weaker models
import the helpers from a module that does not exist (``browser_helpers``)
or end on ``js(...)`` / ``page_info()`` without printing it, get an empty
result, and stall. browser_vault_save_login's upstream next step also says
nothing about browser_vault_fill when the save could not fill the page.

This module is stdlib-only so host tests can import it without Hermes.
"""

from __future__ import annotations

import ast
import re
from typing import Optional

# Helpers named in Hermes' browser_exec description (_HELPERS_DIGEST).
HELPERS = frozenset({
    "new_tab", "goto_url", "wait_for_load", "page_info", "js", "fill_input",
    "click_at_xy", "capture_screenshot", "cdp", "ensure_real_tab",
})

_MISSING_MODULE_RE = re.compile(r"ModuleNotFoundError: No module named '([\w.]+)'")

IMPORT_HINT = (
    "There is no '{module}' module. The browser helpers (new_tab, goto_url, "
    "wait_for_load, page_info, js, fill_input, click_at_xy, capture_screenshot, "
    "cdp, ensure_real_tab) are already defined in every call. Remove the import "
    "and call them directly."
)

NO_OUTPUT_HINT = (
    "The code ran but printed nothing. Only print(...) output comes back, plus "
    "the value of a final expression when it is not None. Print what you need "
    "to read, e.g. print(page_info())."
)

# Upstream text, kept for a fill that worked.
SAVE_FILLED_NEXT = "Type the identifier into the username field if the form has one, then submit."

SAVE_UNFILLED_NEXT = (
    "The login is saved, but this page has no login form, so the password was "
    "not filled. Open the site's sign-in form on this origin, type the "
    "identifier into the username field, then call browser_vault_fill with "
    "handle '{handle}' to fill the password, then submit. Never ask the user "
    "for the password."
)

_ECHO_NAME = "_assistant_stack_value"


def _imports_helpers_from(code: str, module: str) -> bool:
    """True when ``code`` imports helper names from ``module``."""
    top = module.split(".")[0]
    for match in re.finditer(
        r"^\s*from\s+([\w.]+)\s+import\s+\(?([^)\n]*)", code or "", re.MULTILINE
    ):
        if match.group(1).split(".")[0] != top:
            continue
        names = {n.strip().split(" as ")[0] for n in match.group(2).split(",")}
        if names & HELPERS:
            return True
    return False


def result_hint(code: str, exit_code: int, stdout: str, stderr: str) -> Optional[str]:
    """A one-line hint for the model, or None when the result needs none."""
    if exit_code != 0:
        match = _MISSING_MODULE_RE.search(stderr or "")
        if not match:
            return None
        module = match.group(1)
        if _imports_helpers_from(code, module) or re.search(r"browser|helper", module, re.I):
            return IMPORT_HINT.format(module=module)
        return None
    if not (stdout or "").strip() and not (stderr or "").strip():
        return NO_OUTPUT_HINT
    return None


def echo_last_expression(code: str) -> str:
    """Print the value of a trailing bare expression, as a Python prompt does.

    ``js("document.title")`` as the last line comes back as its value instead
    of nothing. Code that already ends in print(...), a statement, or anything
    that does not parse is returned unchanged.
    """
    if not isinstance(code, str) or not code.strip():
        return code
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code
    if not tree.body or not isinstance(tree.body[-1], ast.Expr):
        return code
    last = tree.body[-1]
    value = last.value
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "print":
        return code
    if isinstance(value, ast.Constant):
        return code
    # `a; b` shares a line with the previous statement: leave it alone.
    if last.col_offset != 0 or last.end_lineno is None:
        return code
    segment = ast.get_source_segment(code, last)
    if segment is None:
        return code
    lines = code.splitlines(keepends=True)
    head = "".join(lines[: last.lineno - 1])
    tail = "".join(lines[last.end_lineno:])
    if head and not head.endswith("\n"):
        head += "\n"
    echo = (
        f"{_ECHO_NAME} = (\n{segment}\n)\n"
        f"if {_ECHO_NAME} is not None:\n"
        f"    print({_ECHO_NAME})\n"
    )
    return head + echo + tail


def save_login_next(handle: str, filled: bool) -> str:
    """Next step after browser_vault_save_login stored a login."""
    if filled:
        return SAVE_FILLED_NEXT
    return SAVE_UNFILLED_NEXT.format(handle=handle)
