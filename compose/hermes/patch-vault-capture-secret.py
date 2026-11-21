#!/usr/bin/env python3
"""browser_vault action=capture_secret and the vault.take RPC (CR 0024 R2).

- tools/browser_vault_tool.py: the schema gains the action and its arguments (name, destination,
  selector, candidate), and the dispatcher routes it to tools/vault_capture_secret.py (copied from
  /bootstrap).
- gateway/session_vault_prompt.py: a local viewer's turn also installs the stack's vault prompts
  (vault_prompt_slot.py, an edit shared with patch-vault-create-login.py); capture_secret's sends the
  ``vault.capture_secret`` server request.
- tui_gateway/methods_vault.py: ``vault.take {id}`` returns a capture made for the viewer once and
  deletes it.
- tui_gateway/contracts: the request's and the method's contracts.

The two vault overlays apply in either order: apart from the shared prompt-scope edit, every anchor
here is one patch-vault-create-login.py leaves as it was. Idempotent: a file that holds its marker is
reported as already applied.
"""
from __future__ import annotations

from pathlib import Path

from overlay_patch import ROOT as HERMES, FileEdit, edit_files, install
from vault_prompt_slot import PROMPT_SCOPE_EDITS, PROMPT_SCOPE_MARKER

TAG = "[patch-vault-capture-secret]"
HELPER_SRC = Path("/bootstrap/vault_capture_secret.py")
HELPER_DST = HERMES / "tools" / "vault_capture_secret.py"
VAULT_TOOL = HERMES / "tools" / "browser_vault_tool.py"
PROMPT_SCOPE = HERMES / "gateway" / "session_vault_prompt.py"
SLOT_SRC = Path("/bootstrap/vault_prompt_slot.py")
SLOT_DST = HERMES / "tools" / "vault_prompt_slot.py"
METHODS = HERMES / "tui_gateway" / "methods_vault.py"
REQUEST_CONTRACTS = HERMES / "tui_gateway" / "contracts" / "server_requests.py"
METHOD_CONTRACTS = HERMES / "tui_gateway" / "contracts" / "profiles_vault_complete_foreign_subagents.py"
MARKER = "# assistant-stack: vault capture_secret"

OLD_REGISTER = "from tools.registry import no_cache_check_fn, registry\n"
NEW_REGISTER = (
    "# assistant-stack: vault capture_secret (schema)\n"
    "try:\n"
    "    from tools.vault_capture_secret import extend_schema as _stack_extend_capture_schema\n"
    "    _stack_extend_capture_schema(BROWSER_VAULT_SCHEMA)\n"
    "except Exception:  # health: allow BLE001 -- without the overlay the upstream actions still work\n"
    "    logger.warning(\"vault capture_secret schema overlay failed\", exc_info=True)\n"
    "\n"
    "from tools.registry import no_cache_check_fn, registry\n"
)

OLD_DISPATCH = "    page_ops = {\n"
NEW_DISPATCH = (
    "    page_ops = {\n"
    "        # assistant-stack: vault capture_secret (dispatch)\n"
    "        \"capture_secret\": lambda: __import__(\"tools.vault_capture_secret\", fromlist=[\"handle_action\"])"
    ".handle_action(args, tid),\n"
)


TAKE_METHOD = '''

# assistant-stack: vault capture_secret (take)
@method("vault.take")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Hand a value browser_vault capture_secret stored for the viewer to the client, once.

    Params: ``id`` (the ``capture_id`` the vault.capture_secret request and the tool result carry).
    Result: ``{name, origin, value}``; the capture is deleted as it is read. Unknown, already taken
    and expired (one hour) captures are a 5095 error. The value is never logged."""
    capture_id = str(params.get("id") or "")
    if not capture_id:
        return _err(rid, 5095, "id is required")
    try:
        from tools.vault_capture_secret import take_capture

        found = take_capture(capture_id)
    except Exception:
        return _err(rid, 5095, "taking the capture failed")
    if found is None:
        return _err(rid, 5095, "no capture with that id: already taken, expired or never stored")
    return _ok(rid, found)
'''

REQUEST_CONTRACT = '''

# assistant-stack: vault capture_secret (request contract)
class VaultCaptureSecretRequestParams(ServerRequestParams):
    origin: str
    site: str
    name: str
    destination: str
    last4: str
    replaces: bool = False
    capture_id: str = ""


server_request("vault.capture_secret", params=VaultCaptureSecretRequestParams, result=ValueResult,
               doc=("Approve storing a value the open page shows (an API key) under name, without the model "
                    "seeing it; answer 'approve' or JSON {approve: true}, '' to decline. destination env: the "
                    "profile .env (replaces: an existing value is overwritten); viewer: the client takes it with "
                    "vault.take {id: capture_id}."))
'''

METHOD_CONTRACT = '''

# assistant-stack: vault capture_secret (method contract)
class VaultTakeParams(ProfileParams):
    id: str | None = None


class VaultTakeResult(Result):
    """The one vault result that carries a secret: a capture made for this client, removed as it is read."""

    name: str
    origin: str
    value: str


method("vault.take", params=VaultTakeParams, result=VaultTakeResult,
       doc="Take a value browser_vault capture_secret stored for the viewer; it is deleted as it is returned.")
'''


def main() -> None:
    install(HELPER_SRC, HELPER_DST, TAG)
    install(SLOT_SRC, SLOT_DST, TAG)
    # Every anchor in all five files is checked before any is written. The prompt-scope
    # edit is shared with patch-vault-create-login.py; whichever runs second finds it applied.
    edit_files([
        FileEdit(VAULT_TOOL, [(OLD_REGISTER, NEW_REGISTER), (OLD_DISPATCH, NEW_DISPATCH)],
                 label="capture_secret schema and dispatch"),
        FileEdit(PROMPT_SCOPE, PROMPT_SCOPE_EDITS, label="vault prompt slots", marker=PROMPT_SCOPE_MARKER),
        FileEdit(METHODS, [], label="vault.take method", append=TAKE_METHOD,
                 requires=["@method(\"vault.remove\")", "_profile_scoped"]),
        FileEdit(REQUEST_CONTRACTS, [], label="capture_secret request contract", append=REQUEST_CONTRACT,
                 requires=["class ServerRequestParams", "server_request("]),
        FileEdit(METHOD_CONTRACTS, [], label="vault.take contract", append=METHOD_CONTRACT,
                 requires=["method(\"vault.remove\"", "ProfileParams"]),
    ], MARKER, TAG)


if __name__ == "__main__":
    main()
