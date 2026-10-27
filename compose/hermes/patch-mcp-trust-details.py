#!/usr/bin/env python3
"""Show the call's arguments in MCP trust-gate approvals.

Compatibility overlay (not upstream yet). ``_trust_gate_check`` asks before a
write-capable tool on a ``trust: untrusted`` server runs, but sent only the
tool and server names, so an approval card could not say what the call would
change. Copies mcp_trust_details.py into the tools package and:

- passes the handler's arguments to ``_trust_gate_check`` and on to
  ``_ask_for_call`` (keyword ``args``; v0.22.3 took the third positional
  slot for spend-limit ``booked``);
- adds a redacted one-line argument summary to the prompt text, after any
  spend-limit notice, which also
  stops two different calls to one tool from being coalesced into one prompt;
- adds ``mcp: {server, tool, arguments, tool_call_id}`` and
  ``allow_session: false`` / ``allow_permanent: false`` to the approval entry
  through a new ``details`` keyword on ``request_elicitation_consent``. The
  ``approval`` request and ``approval.pending`` both carry the entry's keys.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
TOOLS = ROOT / "tools"
BOOTSTRAP = Path(os.environ.get("MCP_TRUST_DETAILS_BOOTSTRAP", "/bootstrap"))
HANDLERS = TOOLS / "mcp_tool_handlers.py"
PROMPT = TOOLS / "approval_prompt.py"
MARKER = "# assistant-stack: mcp trust details"
TAG = "[patch-mcp-trust-details]"

SIG_OLD = "def _trust_gate_check(server_name: str, tool_name: str, booked: Optional[list] = None) -> Optional[str]:\n"
SIG_NEW = ("def _trust_gate_check(server_name: str, tool_name: str, booked: Optional[list] = None, *,\n"
           f"                      args: Optional[dict] = None) -> Optional[str]:  {MARKER}\n")

PLAIN_OLD = "        return _ask_for_call(server_name, tool_name, None, booked) if gated else None\n"
PLAIN_NEW = "        return _ask_for_call(server_name, tool_name, None, booked, args=args) if gated else None\n"

COSTED_OLD = "        return _ask_for_call(server_name, tool_name, charge, booked, gated=gated)\n"
COSTED_NEW = "        return _ask_for_call(server_name, tool_name, charge, booked, gated=gated, args=args)\n"

ASK_SIG_OLD = '''                  gated: bool = True) -> Optional[str]:
    """Ask the user about one gated and/or costed call; book the charge only on an explicit yes."""
'''
ASK_SIG_NEW = f'''                  gated: bool = True, args: Optional[dict] = None) -> Optional[str]:
    """Ask the user about one gated and/or costed call; book the charge only on an explicit yes."""
    from tools.approval_context import _approval_tool_call_id  {MARKER}
    from tools.mcp_trust_details import approval_details
    _summary, _details = approval_details(server_name, tool_name, args, _approval_tool_call_id.get())
'''

# The summary goes after the limit notice, which must stay inside the text fallback's first ~200 characters.
GATED_OLD = '''        message = " ".join([head, *(["This call goes past the chat's spend limit."] if over else []), *extra, tail])
'''
GATED_NEW = '''        message = " ".join([head, *(["This call goes past the chat's spend limit."] if over else []),
                            f"Arguments: {_summary}.", *extra, tail])
'''

LIMIT_OLD = '''                            f"spend limit.", *extra])
'''
LIMIT_NEW = '''                            f"spend limit.", f"Arguments: {_summary}.", *extra])
'''

ASK_OLD = '''        answer = request_elicitation_consent(message, description, surface=f"mcp-trust/{server_name}", title=title)
'''
ASK_NEW = '''        answer = request_elicitation_consent(message, description, surface=f"mcp-trust/{server_name}", title=title,
                                             details=_details)
'''

CALL_OLD = "        error = _trust_gate_check(server_name, tool_name, booked)\n"
CALL_NEW = f"        error = _trust_gate_check(server_name, tool_name, booked, args=args)  {MARKER}\n"

KW_OLD = '                                surface: str = "mcp-elicitation", title: str = "Confirm this action?") -> str:\n'
KW_NEW = f'''                                surface: str = "mcp-elicitation", title: str = "Confirm this action?",
                                details: dict | None = None) -> str:  {MARKER}
'''

DATA_OLD = 'notify_cb, {"command": message, "description": description,\n'
# details first: it can add keys to the entry, never replace the command or pattern keys.
DATA_NEW = 'notify_cb, {**(details or {}), "command": message, "description": description,\n'


def patch(path: Path, edits: list[tuple[str, str, str]]) -> None:
    text = path.read_text()
    if MARKER in text:
        print(f"{TAG} already applied ({path.name})")
        return
    for old, _, label in edits:
        if text.count(old) != 1:
            raise SystemExit(f"{TAG} expected 1 x {label} in {path}, found {text.count(old)}. "
                             "Hermes image may have changed — update this patch.")
    for old, new, _ in edits:
        text = text.replace(old, new)
    path.write_text(text)
    print(f"{TAG} patched {path.name}")


def main() -> None:
    src = BOOTSTRAP / "mcp_trust_details.py"
    if not src.is_file():
        raise SystemExit(f"{TAG} missing {src}")
    # Check both targets before touching either, so a moved anchor leaves Hermes as shipped.
    handler_edits = [(SIG_OLD, SIG_NEW, "_trust_gate_check signature"),
                     (PLAIN_OLD, PLAIN_NEW, "uncosted ask"),
                     (COSTED_OLD, COSTED_NEW, "costed ask"),
                     (ASK_SIG_OLD, ASK_SIG_NEW, "_ask_for_call signature"),
                     (GATED_OLD, GATED_NEW, "trust gate prompt"),
                     (LIMIT_OLD, LIMIT_NEW, "spend limit prompt"),
                     (ASK_OLD, ASK_NEW, "consent call"),
                     (CALL_OLD, CALL_NEW, "handler gate call")]
    for path, anchors in ((PROMPT, (KW_OLD, DATA_OLD)), (HANDLERS, [old for old, _, _ in handler_edits])):
        text = path.read_text()
        if MARKER not in text and any(text.count(a) != 1 for a in anchors):
            raise SystemExit(f"{TAG} anchors missing in {path}. Hermes image may have changed — update this patch.")
    shutil.copyfile(src, TOOLS / "mcp_trust_details.py")
    print(f"{TAG} installed {TOOLS / 'mcp_trust_details.py'}")
    patch(PROMPT, [(KW_OLD, KW_NEW, "request_elicitation_consent signature"),
                   (DATA_OLD, DATA_NEW, "gateway approval data")])
    patch(HANDLERS, handler_edits)


if __name__ == "__main__":
    main()
