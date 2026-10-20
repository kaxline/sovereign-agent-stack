#!/usr/bin/env python3
"""Show the call's arguments in MCP trust-gate approvals.

Compatibility overlay (not upstream yet). ``_trust_gate_check`` asks before a
write-capable tool on a ``trust: untrusted`` server runs, but sent only the
tool and server names, so an approval card could not say what the call would
change. Copies mcp_trust_details.py into the tools package and:

- passes the handler's arguments to ``_trust_gate_check``;
- adds a redacted one-line argument summary to the prompt text, which also
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

SIG_OLD = "def _trust_gate_check(server_name: str, tool_name: str) -> Optional[str]:\n"
SIG_NEW = f"def _trust_gate_check(server_name: str, tool_name: str, args: Optional[dict] = None) -> Optional[str]:  {MARKER}\n"

ASK_OLD = '''        from tools.approval_prompt import request_elicitation_consent
        answer = request_elicitation_consent(
            f"MCP tool '{tool_name}' on UNTRUSTED server '{server_name}' wants to run. This tool is write-capable "
            f"(no readOnlyHint=true annotation) and may modify external state.",
            f"Server '{server_name}' is configured 'trust: untrusted'. "
            f"Approve to run '{tool_name}' once, or deny to block it.",
            surface=f"mcp-trust/{server_name}", title=f"MCP server '{server_name}' is asking")
'''
ASK_NEW = f'''        from tools.approval_prompt import request_elicitation_consent
        from tools.approval_context import _approval_tool_call_id  {MARKER}
        from tools.mcp_trust_details import approval_details
        _summary, _details = approval_details(server_name, tool_name, args, _approval_tool_call_id.get())
        answer = request_elicitation_consent(
            f"MCP tool '{{tool_name}}' on UNTRUSTED server '{{server_name}}' wants to run with {{_summary}}. "
            f"This tool is write-capable (no readOnlyHint=true annotation) and may modify external state.",
            f"Server '{{server_name}}' is configured 'trust: untrusted'. "
            f"Approve to run '{{tool_name}}' once, or deny to block it.",
            surface=f"mcp-trust/{{server_name}}", title=f"MCP server '{{server_name}}' is asking", details=_details)
'''

CALL_OLD = "        error = _trust_gate_check(server_name, tool_name) or _check_circuit_breaker(server_name)\n"
CALL_NEW = f"        error = _trust_gate_check(server_name, tool_name, args) or _check_circuit_breaker(server_name)  {MARKER}\n"

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
    for path, anchors in ((PROMPT, (KW_OLD, DATA_OLD)), (HANDLERS, (SIG_OLD, ASK_OLD, CALL_OLD))):
        text = path.read_text()
        if MARKER not in text and any(text.count(a) != 1 for a in anchors):
            raise SystemExit(f"{TAG} anchors missing in {path}. Hermes image may have changed — update this patch.")
    shutil.copyfile(src, TOOLS / "mcp_trust_details.py")
    print(f"{TAG} installed {TOOLS / 'mcp_trust_details.py'}")
    patch(PROMPT, [(KW_OLD, KW_NEW, "request_elicitation_consent signature"),
                   (DATA_OLD, DATA_NEW, "gateway approval data")])
    patch(HANDLERS, [(SIG_OLD, SIG_NEW, "_trust_gate_check signature"),
                     (ASK_OLD, ASK_NEW, "trust gate prompt"),
                     (CALL_OLD, CALL_NEW, "handler gate call")])


if __name__ == "__main__":
    main()
