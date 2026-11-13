#!/usr/bin/env python3
"""browser_vault sign-in actions for bwbroker: logins (CR 0033).

tools/browser_vault_tool.py gains two things: the schema hook that adds the
seven actions and their arguments, and one dispatch entry per action. Both call
tools/vault_bitwarden_signin.py, copied here from /bootstrap, which holds the
stack's side; the upstream file keeps the fork cap's line budget.

The dispatch entries sit in ``page_ops``, so a sign-in runs under the same Bot
Desktop lease fence as a fill. Without the module the names are simply absent
and the upstream actions still work.

Runs after patch-bitwarden-broker-fill.py, whose helpers the module calls.
Idempotent; each part reports ok / patched / warn.
"""
from __future__ import annotations

import shutil
from pathlib import Path

HERMES = Path("/opt/hermes")
HELPER_SRC = Path("/bootstrap/vault_bitwarden_signin.py")
HELPER_DST = HERMES / "tools" / "vault_bitwarden_signin.py"
VAULT_TOOL = HERMES / "tools" / "browser_vault_tool.py"
MARKER = "# assistant-stack: bitwarden sign-in"

OLD_SCHEMA = "from tools.registry import no_cache_check_fn, registry\n"
NEW_SCHEMA = (
    f"{MARKER} (schema)\n"
    "try:\n"
    "    from tools import vault_bitwarden_signin as _stack_signin\n"
    "    _stack_signin.extend_schema(BROWSER_VAULT_SCHEMA)\n"
    "except Exception:  # health: allow BLE001 -- without the overlay the upstream actions still work\n"
    "    _stack_signin = None\n"
    "    logger.warning(\"bitwarden sign-in schema overlay failed\", exc_info=True)\n"
    "\n"
    "from tools.registry import no_cache_check_fn, registry\n"
)

OLD_DISPATCH = (
    "    }\n"
    "    if action not in page_ops:\n"
)
NEW_DISPATCH = (
    f"        {MARKER} (dispatch)\n"
    "        **({a: (lambda a=a: _stack_signin.handle_action(a, args, tid)) for a in _stack_signin.ACTIONS}\n"
    "           if _stack_signin is not None else {}),\n"
    "    }\n"
    "    if action not in page_ops:\n"
)


def _apply(text: str, old: str, new: str, marker: str, label: str, path: Path) -> str:
    if marker in text:
        print(f"ok: {label} already patched")
        return text
    if old not in text:
        print(f"warn: {label} needle missing in {path}")
        return text
    print(f"patched: {label} in {path}")
    return text.replace(old, new, 1)


def main() -> None:
    if not HELPER_SRC.is_file():
        print(f"warn: {HELPER_SRC} missing; browser_vault sign-in off")
        return
    if not VAULT_TOOL.exists():
        print(f"skip: {VAULT_TOOL} missing")
        return
    shutil.copyfile(HELPER_SRC, HELPER_DST)
    print(f"installed: {HELPER_DST}")
    text = VAULT_TOOL.read_text()
    patched = _apply(text, OLD_SCHEMA, NEW_SCHEMA, f"{MARKER} (schema)", "sign-in schema", VAULT_TOOL)
    patched = _apply(patched, OLD_DISPATCH, NEW_DISPATCH, f"{MARKER} (dispatch)", "sign-in dispatch", VAULT_TOOL)
    if patched != text:
        VAULT_TOOL.write_text(patched)


if __name__ == "__main__":
    main()
