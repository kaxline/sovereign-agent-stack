"""Hints added to a browser_exec result when the code misused the sandbox.

Compatibility overlay for this stack. browser_exec runs `code` with the
browser helpers already defined and returns stdout only. Weaker models
import the helpers from a module that does not exist (``browser_helpers``)
or call ``page_info()`` without printing it, get an empty result, and stall.

This module is stdlib-only so host tests can import it without Hermes.
"""

from __future__ import annotations

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
    "The code ran but printed nothing. Only print(...) output comes back; a "
    "bare expression such as page_info() is not shown. Use print(page_info())."
)


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
