#!/usr/bin/env python3
"""Fix the hints browser_exec gives the model.

- browser_exec wraps every local-browser startup failure with a Chromium
  reinstall hint. EACCES on agent-browser-linux-* is a mode bit on the CLI
  binary, and the Browser Automation menu no-ops once Chromium is in the image.
- A result gains a ``hint`` when the code imported the pre-defined helpers
  from a module that does not exist, or ran and printed nothing (see
  browser_exec_hints.py). Copies that helper into /opt/hermes/tools/.
"""
from __future__ import annotations

import shutil
from pathlib import Path

TARGET = Path("/opt/hermes/tools/browser_use_cli.py")
MARKER = "# assistant-stack: agent-browser EACCES is not a Chromium install"
HELPER_SRC = Path("/bootstrap/browser_exec_hints.py")
HELPER_DST = Path("/opt/hermes/tools/browser_exec_hints.py")
MARKER_RESULT = "# assistant-stack: browser_exec result hints"

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

OLD_RESULT = (
    "    if stderr:\n"
    "        result[\"stderr\"] = stderr\n"
    "    screenshot = _find_screenshot(proc.stdout, started)\n"
)

NEW_RESULT = (
    "    if stderr:\n"
    "        result[\"stderr\"] = stderr\n"
    "    # assistant-stack: browser_exec result hints\n"
    "    try:\n"
    "        from tools.browser_exec_hints import result_hint\n"
    "        _exec_hint = result_hint(code, proc.returncode, proc.stdout, stderr)\n"
    "    except Exception:\n"
    "        logger.debug(\"browser_exec hint failed\", exc_info=True)\n"
    "        _exec_hint = None\n"
    "    if _exec_hint:\n"
    "        result[\"hint\"] = _exec_hint\n"
    "    screenshot = _find_screenshot(proc.stdout, started)\n"
)


def _apply(text: str, old: str, new: str, marker: str, label: str) -> str:
    if marker in text:
        print(f"ok: {label} already patched")
        return text
    if old not in text:
        print(f"warn: {label} needle missing in {TARGET}")
        return text
    print(f"patched: {label} in {TARGET}")
    return text.replace(old, new, 1)


def main() -> None:
    if not TARGET.exists():
        print(f"skip: {TARGET} missing")
        return
    if HELPER_SRC.is_file():
        shutil.copyfile(HELPER_SRC, HELPER_DST)
        print(f"installed: {HELPER_DST}")
    else:
        print(f"warn: {HELPER_SRC} missing; result hints off")
    text = TARGET.read_text()
    patched = _apply(text, OLD, NEW, MARKER, "EACCES hint")
    patched = _apply(patched, OLD_RESULT, NEW_RESULT, MARKER_RESULT, "result hints")
    if patched != text:
        TARGET.write_text(patched)


if __name__ == "__main__":
    main()
