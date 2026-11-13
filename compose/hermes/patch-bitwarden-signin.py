#!/usr/bin/env python3
"""browser_vault sign-in actions for bwbroker: logins (CR 0033).

tools/browser_vault_tool.py gains two things: the schema hook that adds the
seven actions and their arguments, and one dispatch entry per action. Both call
tools/vault_bitwarden_signin.py, copied here from /bootstrap, which holds the
stack's side; the upstream file keeps the fork cap's line budget.

The dispatch entries sit in ``page_ops``, so a sign-in runs under the same Bot
Desktop lease fence as a fill. They name the actions themselves and import the
module inside the lambda, so neither a module that fails to import nor a schema
hook that did not apply can break ``page_ops``: the upstream actions keep
working and a sign-in action is what fails.

Runs after patch-bitwarden-broker-fill.py, whose helpers the module calls.
Idempotent; each part reports ok / patched / warn.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
HELPER_SRC = Path("/bootstrap/vault_bitwarden_signin.py")
HELPER_DST = HERMES / "tools" / "vault_bitwarden_signin.py"
VAULT_TOOL = HERMES / "tools" / "browser_vault_tool.py"
MARKER = "# assistant-stack: bitwarden sign-in"
ACTIONS = ("sign_in", "signin_snapshot", "signin_click", "signin_type", "signin_fill",
           "signin_finish", "signin_cancel")

OLD_SCHEMA = "from tools.registry import no_cache_check_fn, registry\n"
NEW_SCHEMA = (
    f"{MARKER} (schema)\n"
    "try:\n"
    "    from tools.vault_bitwarden_signin import extend_schema as _stack_extend_signin_schema\n"
    "    _stack_extend_signin_schema(BROWSER_VAULT_SCHEMA)\n"
    "except Exception:  # health: allow BLE001 -- without the overlay the upstream actions still work\n"
    "    logger.warning(\"bitwarden sign-in schema overlay failed\", exc_info=True)\n"
    "\n"
    "from tools.registry import no_cache_check_fn, registry\n"
)

# The action names are repeated here, not read from the module: building page_ops must not depend
# on the import, so a missing or broken module costs the sign-in actions alone. The import sits
# inside the lambda, as the other vault overlays do.
OLD_DISPATCH = (
    "    }\n"
    "    if action not in page_ops:\n"
)
NEW_DISPATCH = (
    f"        {MARKER} (dispatch)\n"
    "        **{a: (lambda a=a: __import__(\"tools.vault_bitwarden_signin\", fromlist=[\"handle_action\"])\n"
    "               .handle_action(a, args, tid))\n"
    "           for a in (\"sign_in\", \"signin_snapshot\", \"signin_click\", \"signin_type\", \"signin_fill\",\n"
    "                     \"signin_finish\", \"signin_cancel\")},\n"
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
    helper = HELPER_SRC.read_text()
    missing = [a for a in ACTIONS if f'"{a}"' not in helper]
    if missing:
        print(f"warn: {HELPER_SRC} has no {missing}; browser_vault sign-in off")
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
