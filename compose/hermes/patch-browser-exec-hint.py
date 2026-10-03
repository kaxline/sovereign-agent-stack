#!/usr/bin/env python3
"""Don't tell the model to reinstall Chromium when agent-browser cannot exec.

browser_exec wraps every local-browser startup failure with a Chromium
reinstall hint. EACCES on agent-browser-linux-* is a mode bit on the CLI
binary, and the Browser Automation menu no-ops once Chromium is in the image.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("/opt/hermes/tools/browser_use_cli.py")
MARKER = "# assistant-stack: agent-browser EACCES is not a Chromium install"

OLD = (
    "    if not cdp:\n"
    "        return (f\"The local browser could not be started: "
    "{(res or {}).get('error') or 'agent-browser returned no CDP endpoint'} \"\n"
    "                \"Run `hermes tools` → Browser Automation to (re)install Chromium, "
    "or switch backends.\")\n"
)

NEW = (
    "    if not cdp:\n"
    "        # assistant-stack: agent-browser EACCES is not a Chromium install\n"
    "        _browser_err = (res or {}).get(\"error\") or "
    "\"agent-browser returned no CDP endpoint\"\n"
    "        if \"EACCES\" in _browser_err and \"agent-browser\" in _browser_err:\n"
    "            _browser_hint = (\n"
    "                \"The agent-browser CLI binary is not executable. \"\n"
    "                \"Reinstalling Chromium will not fix this. \"\n"
    "                \"Restart the hermes container so the boot install can place \"\n"
    "                \"agent-browser on the container filesystem, then retry.\"\n"
    "            )\n"
    "        else:\n"
    "            _browser_hint = (\n"
    "                \"Run `hermes tools` → Browser Automation to (re)install Chromium, \"\n"
    "                \"or switch backends.\"\n"
    "            )\n"
    "        return f\"The local browser could not be started: {_browser_err} {_browser_hint}\"\n"
)


def main() -> None:
    if not TARGET.exists():
        print(f"skip: {TARGET} missing")
        return
    text = TARGET.read_text()
    if MARKER in text:
        print(f"ok: {TARGET} already patched")
        return
    if OLD not in text:
        print(f"warn: browser_exec hint needle missing in {TARGET}")
        return
    TARGET.write_text(text.replace(OLD, NEW, 1))
    print(f"patched: {TARGET}")


if __name__ == "__main__":
    main()
