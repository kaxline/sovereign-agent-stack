#!/usr/bin/env python3
"""browser_vault sign-in actions for bwbroker: logins.

Design: docs/superpowers/specs/2026-11-13-bitwarden-signin-design.md.

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
Idempotent. Each part degrades on its own (optional=True): a missing anchor prints a
skip line and the script still exits 0 (docs/hermes.md).
"""
from __future__ import annotations

from pathlib import Path

from overlay_patch import ROOT as HERMES, edit, fail, install

TAG = "[patch-bitwarden-signin]"
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


def main() -> None:
    helper = HELPER_SRC.read_text() if HELPER_SRC.is_file() else ""
    missing = [a for a in ACTIONS if f'"{a}"' not in helper]
    if missing:
        fail(TAG, f"{HELPER_SRC} has no {missing}: browser_vault sign-in off", optional=True)
        return
    install(HELPER_SRC, HELPER_DST, TAG)
    edit(VAULT_TOOL, [(OLD_SCHEMA, NEW_SCHEMA)], f"{MARKER} (schema)", TAG, optional=True, label="sign-in schema")
    edit(VAULT_TOOL, [(OLD_DISPATCH, NEW_DISPATCH)], f"{MARKER} (dispatch)", TAG, optional=True,
         label="sign-in dispatch")


if __name__ == "__main__":
    main()
