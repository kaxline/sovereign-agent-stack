#!/usr/bin/env python3
"""Pin agent-browser npx to a concrete version and use --no-install after warmup.

Cold `npx -y agent-browser@^0.26.0` reifies on the Docker bind-mounted npm cache
and hits npm ECOMPROMISED ("Lock compromised"). Cont-init 08 warms the cache;
this patch refuses implicit registry fetches at tool time.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("/opt/hermes/tools/browser_tool.py")
SESSION = Path("/opt/hermes/tools/browser_tool_session.py")
MARKER = "# assistant-stack: pin agent-browser npx --no-install"


def patch_spec() -> None:
    if not TARGET.exists():
        print(f"skip: {TARGET} missing")
        return
    text = TARGET.read_text()
    if MARKER in text:
        print(f"ok: {TARGET} already patched")
        return
    old = 'AGENT_BROWSER_NPX_SPEC = "agent-browser@^0.26.0"'
    # Prefer exact pin; fall back to whatever caret/range the image ships.
    if old not in text:
        import re

        m = re.search(r'AGENT_BROWSER_NPX_SPEC = "([^"]+)"', text)
        if not m:
            print(f"warn: AGENT_BROWSER_NPX_SPEC not found in {TARGET}")
            return
        old = m.group(0)
    new = (
        'AGENT_BROWSER_NPX_SPEC = "agent-browser@0.26.0"  '
        f"{MARKER}"
    )
    text = text.replace(old, new, 1)
    TARGET.write_text(text)
    print(f"patched: {TARGET} spec")


def patch_argv() -> None:
    if not SESSION.exists():
        print(f"skip: {SESSION} missing")
        return
    text = SESSION.read_text()
    if "assistant-stack: pin agent-browser" in text and "--no-install" in text:
        print(f"ok: {SESSION} already patched")
        return
    old = (
        '        return [_npx_bin, "--ignore-scripts", "--prefer-offline", "-y", '
        "_bt.AGENT_BROWSER_NPX_SPEC]"
    )
    new = (
        "        # assistant-stack: pin agent-browser npx --no-install\n"
        "        # Cont-init 08-warmup-agent-browser must have populated the npx cache.\n"
        '        return [_npx_bin, "--no-install", "--ignore-scripts", '
        '"--prefer-offline", _bt.AGENT_BROWSER_NPX_SPEC]'
    )
    if old not in text:
        print(f"warn: npx argv needle missing in {SESSION}")
        return
    SESSION.write_text(text.replace(old, new, 1))
    print(f"patched: {SESSION} argv")


def main() -> None:
    patch_spec()
    patch_argv()


if __name__ == "__main__":
    main()
