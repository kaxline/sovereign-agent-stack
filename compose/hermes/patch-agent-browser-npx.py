#!/usr/bin/env python3
"""Pin agent-browser npx to a concrete version and use --no-install.

The primary CLI is the overlay install from cont-init 08 (on PATH, off the
data/hermes bind mount). This patch is the fallback when that binary is
absent: cold `npx -y agent-browser@^0.26.0` reifies on the Docker bind-mounted
npm cache and hits npm ECOMPROMISED ("Lock compromised").
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
    old_comment = (
        "# Cont-init 08-warmup-agent-browser must have populated the npx cache."
    )
    new_comment = (
        "# Overlay install on PATH is primary; this npx argv is the fallback."
    )
    if "assistant-stack: pin agent-browser" in text and "--no-install" in text:
        if old_comment in text:
            SESSION.write_text(text.replace(old_comment, new_comment, 1))
            print(f"patched: {SESSION} comment")
        else:
            print(f"ok: {SESSION} already patched")
        return
    old = (
        '        return [_npx_bin, "--ignore-scripts", "--prefer-offline", "-y", '
        "_bt.AGENT_BROWSER_NPX_SPEC]"
    )
    new = (
        "        # assistant-stack: pin agent-browser npx --no-install\n"
        f"        {new_comment}\n"
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
