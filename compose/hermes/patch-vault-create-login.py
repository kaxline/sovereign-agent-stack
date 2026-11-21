#!/usr/bin/env python3
"""browser_vault action=create_login (CR 0024 R1): a sign-up login with a generated password.

- tools/browser_vault_tool.py: the schema gains the action and an ``identifier`` argument, and the
  dispatcher routes it to tools/vault_create_login.py (copied from /bootstrap).
- gateway/session_vault_prompt.py: a local viewer's turn also installs the stack's vault prompts
  (vault_prompt_slot.py, an edit shared with patch-vault-capture-secret.py); create_login's sends the
  ``vault.create_login`` server request ``{origin, site, identifier, regenerate}``.
- tui_gateway/contracts/server_requests.py: the request's contract, so it is advertised in the
  ``server_requests`` list a client reads at connect time.

Idempotent: a file that holds its marker is reported as already applied.
"""
from __future__ import annotations

from pathlib import Path

from overlay_patch import ROOT as HERMES, FileEdit, edit_files, install
from vault_prompt_slot import PROMPT_SCOPE_EDITS, PROMPT_SCOPE_MARKER

TAG = "[patch-vault-create-login]"
HELPER_SRC = Path("/bootstrap/vault_create_login.py")
HELPER_DST = HERMES / "tools" / "vault_create_login.py"
VAULT_TOOL = HERMES / "tools" / "browser_vault_tool.py"
PROMPT_SCOPE = HERMES / "gateway" / "session_vault_prompt.py"
SLOT_SRC = Path("/bootstrap/vault_prompt_slot.py")
SLOT_DST = HERMES / "tools" / "vault_prompt_slot.py"
CONTRACTS = HERMES / "tui_gateway" / "contracts" / "server_requests.py"
MARKER = "# assistant-stack: vault create_login"

OLD_REGISTER = "from tools.registry import no_cache_check_fn, registry\n"
NEW_REGISTER = (
    "# assistant-stack: vault create_login (schema)\n"
    "try:\n"
    "    from tools.vault_create_login import extend_schema as _stack_extend_vault_schema\n"
    "    _stack_extend_vault_schema(BROWSER_VAULT_SCHEMA)\n"
    "except Exception:  # health: allow BLE001 -- without the overlay the upstream actions still work\n"
    "    logger.warning(\"vault create_login schema overlay failed\", exc_info=True)\n"
    "\n"
    "from tools.registry import no_cache_check_fn, registry\n"
)

OLD_DISPATCH = (
    "        \"enter_code\": lambda: browser_vault_enter_code(handle=handle, task_id=tid),\n"
    "    }\n"
)
NEW_DISPATCH = (
    "        \"enter_code\": lambda: browser_vault_enter_code(handle=handle, task_id=tid),\n"
    "        # assistant-stack: vault create_login (dispatch)\n"
    "        \"create_login\": lambda: __import__(\"tools.vault_create_login\", fromlist=[\"handle_action\"])"
    ".handle_action(args, tid),\n"
    "    }\n"
)


CONTRACT = '''

# assistant-stack: vault create_login (contract)
class VaultCreateLoginRequestParams(ServerRequestParams):
    origin: str
    site: str
    identifier: str
    regenerate: bool = False


server_request("vault.create_login", params=VaultCreateLoginRequestParams, result=ValueResult,
               doc=("Approve a new login for a sign-up form; the answer is JSON {generate: true} for a generated "
                    "password or {password} for one the user typed, '' to decline. regenerate: replace the "
                    "password of a login made earlier for this site."))
'''


def main() -> None:
    install(HELPER_SRC, HELPER_DST, TAG)
    install(SLOT_SRC, SLOT_DST, TAG)
    # Every anchor in all three files is checked before any is written. The prompt-scope
    # edit is shared with patch-vault-capture-secret.py; whichever runs second finds it applied.
    edit_files([
        FileEdit(VAULT_TOOL, [(OLD_REGISTER, NEW_REGISTER), (OLD_DISPATCH, NEW_DISPATCH)],
                 label="create_login schema and dispatch"),
        FileEdit(PROMPT_SCOPE, PROMPT_SCOPE_EDITS, label="vault prompt slots", marker=PROMPT_SCOPE_MARKER),
        FileEdit(CONTRACTS, [], label="create_login contract", append=CONTRACT,
                 requires=["class ServerRequestParams", "server_request("]),
    ], MARKER, TAG)


if __name__ == "__main__":
    main()
