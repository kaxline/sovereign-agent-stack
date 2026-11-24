#!/usr/bin/env python3
"""Put the gated call's details on MCP trust-gate approval entries.

Compatibility overlay (not upstream yet). Since v0.22.7 Hermes shows a
redacted argument block in the trust-gate prompt and remembers Session/Always
answers per server and tool, which this overlay used to work around. What it
still adds is the structured ``mcp: {server, tool, arguments, tool_call_id}``
field that docs/extending.md promises: ``_ask_for_call`` builds it with
mcp_trust_details.py and hands it to ``request_elicitation_consent`` through a
new ``details`` keyword, which merges it into the gateway approval entry. The
``approval`` request and ``approval.pending`` both carry the entry's keys.
"""
from __future__ import annotations

from overlay_patch import BOOTSTRAP, ROOT, FileEdit, edit_files, install

TOOLS = ROOT / "tools"
HANDLERS = TOOLS / "mcp_tool_handlers.py"
PROMPT = TOOLS / "approval_prompt.py"
MARKER = "# assistant-stack: mcp trust details"
TAG = "[patch-mcp-trust-details]"

ASK_SIG_OLD = '''                  gated: bool = True, args: Optional[dict] = None) -> Optional[str]:
    """Ask the user about one gated and/or costed call; book the charge only on an explicit yes."""
'''
ASK_SIG_NEW = f'''                  gated: bool = True, args: Optional[dict] = None) -> Optional[str]:
    """Ask the user about one gated and/or costed call; book the charge only on an explicit yes."""
    from tools.approval_context import _approval_tool_call_id  {MARKER}
    from tools.mcp_trust_details import approval_details
    _details = approval_details(server_name, tool_name, args, _approval_tool_call_id.get())
'''

ASK_OLD = '''        answer = request_elicitation_consent(message, description, surface=f"mcp-trust/{server_name}", title=title,
                                            remember_key=remember_key)
'''
ASK_NEW = '''        answer = request_elicitation_consent(message, description, surface=f"mcp-trust/{server_name}", title=title,
                                            remember_key=remember_key, details=_details)
'''

KW_OLD = '''                                surface: str = "mcp-elicitation", title: str = "Confirm this action?",
                                remember_key: str | None = None) -> str:
'''
KW_NEW = f'''                                surface: str = "mcp-elicitation", title: str = "Confirm this action?",
                                remember_key: str | None = None, details: dict | None = None) -> str:  {MARKER}
'''

DATA_OLD = 'notify_cb, {"command": message, "description": description,\n'
# details first: it can add keys to the entry, never replace the command, pattern or scope keys.
DATA_NEW = 'notify_cb, {**(details or {}), "command": message, "description": description,\n'


def main() -> None:
    files = [
        FileEdit(PROMPT, [(KW_OLD, KW_NEW), (DATA_OLD, DATA_NEW)], label=PROMPT.name),
        FileEdit(HANDLERS, [(ASK_SIG_OLD, ASK_SIG_NEW), (ASK_OLD, ASK_NEW)], label=HANDLERS.name),
    ]
    install(BOOTSTRAP / "mcp_trust_details.py", TOOLS / "mcp_trust_details.py", TAG)
    # Both targets are checked before either is written, so a moved anchor leaves Hermes as shipped.
    edit_files(files, MARKER, TAG)


if __name__ == "__main__":
    main()
