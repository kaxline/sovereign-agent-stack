"""Hermes tui_gateway error codes → ACP errors with a stack reason.

From ``tui_gateway/methods_prompt.py`` and the stack overlays at v0.23.1
(spike 0.3). 4004, 4018 and 4030 are reused by other methods, so the table is
keyed on method and code; a code with no row for its method falls to
``runtime_error`` with Hermes' code in ``runtime_code``. No Hermes number is
sent to a client any other way.
"""

from __future__ import annotations

from ..errors import INTERNAL_ERROR, INVALID_PARAMS, RESOURCE_NOT_FOUND, AcpError

# (method, code) -> (ACP code, reason). method "*" matches any method.
TABLE: dict[tuple[str, int], tuple[int, str]] = {
    ("*", 4080): (INVALID_PARAMS, "context_invalid"),
    ("*", 4081): (INVALID_PARAMS, "profile_mismatch"),
    ("*", 4082): (INVALID_PARAMS, "context_while_busy"),
    ("*", 4083): (INVALID_PARAMS, "context_unsupported"),
    ("prompt.submit", 4004): (INVALID_PARAMS, "rewind_invalid"),
    ("prompt.submit", 4018): (INVALID_PARAMS, "rewind_target_not_found"),
    ("prompt.submit", 4030): (INVALID_PARAMS, "rewind_stale"),
    ("prompt.submit", 4028): (INVALID_PARAMS, "rewind_empties_session"),
    ("*", 4087): (INVALID_PARAMS, "history_budget_invalid"),
    ("session.resume", 4007): (RESOURCE_NOT_FOUND, "session_not_found"),
    ("session.delete", 4007): (RESOURCE_NOT_FOUND, "session_not_found"),
    ("session.delete", 4023): (INVALID_PARAMS, "session_busy"),
    ("prompt.submit", 4009): (INVALID_PARAMS, "session_busy"),
    ("*", 4090): (INTERNAL_ERROR, "runtime_busy"),
}


def to_acp(method: str, code: int, message: str = "") -> AcpError:
    hit = TABLE.get((method, code)) or TABLE.get(("*", code))
    if hit is None:
        return AcpError(INTERNAL_ERROR, "runtime error", "runtime_error", runtime_code=code)
    acp_code, reason = hit
    if reason == "runtime_busy":
        return AcpError(acp_code, "no free session slot", reason, runtime_code=code)
    return AcpError(acp_code, reason.replace("_", " "), reason)
