#!/usr/bin/env python3
"""browser_vault action=capture_secret and the vault.take RPC (CR 0024 R2).

- tools/browser_vault_tool.py: the schema gains the action and its arguments (name, destination,
  selector, candidate), and the dispatcher routes it to tools/vault_capture_secret.py (copied from
  /bootstrap).
- gateway/session_vault_prompt.py: a local viewer's turn also installs the approval prompt, which sends
  the ``vault.capture_secret`` server request.
- tui_gateway/methods_vault.py: ``vault.take {id}`` returns a capture made for the viewer once and
  deletes it.
- tui_gateway/contracts: the request's and the method's contracts.

Independent of patch-vault-create-login.py: every anchor here is one that patch leaves as it was, so
the two apply in either order. Idempotent; each part reports ok / patched / warn on its own.
"""
from __future__ import annotations

import shutil
from pathlib import Path

HERMES = Path("/opt/hermes")
HELPER_SRC = Path("/bootstrap/vault_capture_secret.py")
HELPER_DST = HERMES / "tools" / "vault_capture_secret.py"
VAULT_TOOL = HERMES / "tools" / "browser_vault_tool.py"
PROMPT_SCOPE = HERMES / "gateway" / "session_vault_prompt.py"
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

OLD_CODE_PROMPT = "    def code_prompt(site, hint):\n"
NEW_CODE_PROMPT = (
    "    # assistant-stack: vault capture_secret (prompt)\n"
    "    def capture_secret_prompt(**params):\n"
    "        from tools.vault_capture_secret import PROMPT_METHOD, PROMPT_TIMEOUT_S, parse_answer\n"
    "        return parse_answer(ask(PROMPT_METHOD, params, PROMPT_TIMEOUT_S))\n"
    "\n"
    "    try:\n"
    "        from tools.vault_capture_secret import reset_prompt_callback as _stack_reset_capture\n"
    "        from tools.vault_capture_secret import set_prompt_callback as _stack_set_capture\n"
    "    except Exception:  # health: allow BLE001 -- capture_secret then reports prompt_unavailable\n"
    "        logger.debug(\"vault capture_secret prompt overlay unavailable\", exc_info=True)\n"
    "        _stack_set_capture = _stack_reset_capture = None\n"
    "\n"
    "    def code_prompt(site, hint):\n"
)

OLD_INSTALL = "    unlock.set_code_prompt_callback(code_prompt)\n"
NEW_INSTALL = (
    "    _stack_capture_token = _stack_set_capture(capture_secret_prompt) if _stack_set_capture else None\n"
    "    unlock.set_code_prompt_callback(code_prompt)\n"
)

OLD_RESTORE = "        unlock.set_code_prompt_callback(previous[2])\n"
NEW_RESTORE = (
    "        if _stack_capture_token is not None:\n"
    "            _stack_reset_capture(_stack_capture_token)\n"
    "        unlock.set_code_prompt_callback(previous[2])\n"
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


def _apply(text: str, old: str, new: str, marker: str, label: str, path: Path) -> str:
    if marker in text:
        print(f"ok: {label} already patched")
        return text
    if old not in text:
        print(f"warn: {label} needle missing in {path}")
        return text
    print(f"patched: {label} in {path}")
    return text.replace(old, new, 1)


def _patch_file(path: Path, steps: list[tuple[str, str, str, str]]) -> None:
    if not path.exists():
        print(f"skip: {path} missing")
        return
    text = path.read_text()
    patched = text
    for old, new, marker, label in steps:
        patched = _apply(patched, old, new, marker, label, path)
    if patched != text:
        path.write_text(patched)


def _append(path: Path, block: str, marker: str, anchors: tuple[str, ...], label: str) -> None:
    if not path.exists():
        print(f"skip: {path} missing")
        return
    text = path.read_text()
    if marker in text:
        print(f"ok: {label} already patched")
    elif all(a in text for a in anchors):
        path.write_text(text.rstrip("\n") + "\n" + block)
        print(f"patched: {label} in {path}")
    else:
        print(f"warn: {label} anchors missing in {path}")


def main() -> None:
    if not HELPER_SRC.is_file():
        print(f"warn: {HELPER_SRC} missing; capture_secret off")
        return
    shutil.copyfile(HELPER_SRC, HELPER_DST)
    print(f"installed: {HELPER_DST}")
    _patch_file(VAULT_TOOL, [
        (OLD_REGISTER, NEW_REGISTER, f"{MARKER} (schema)", "capture_secret schema"),
        (OLD_DISPATCH, NEW_DISPATCH, f"{MARKER} (dispatch)", "capture_secret dispatch"),
    ])
    # The prompt's three edits are one unit: install without restore would leak the callback.
    if PROMPT_SCOPE.exists():
        text = PROMPT_SCOPE.read_text()
        if f"{MARKER} (prompt)" in text:
            print("ok: capture_secret prompt already patched")
        elif all(text.count(old) == 1 for old in (OLD_CODE_PROMPT, OLD_INSTALL, OLD_RESTORE)):
            text = (text.replace(OLD_CODE_PROMPT, NEW_CODE_PROMPT, 1).replace(OLD_INSTALL, NEW_INSTALL, 1)
                    .replace(OLD_RESTORE, NEW_RESTORE, 1))
            PROMPT_SCOPE.write_text(text)
            print(f"patched: capture_secret prompt in {PROMPT_SCOPE}")
        else:
            print(f"warn: capture_secret prompt needles missing in {PROMPT_SCOPE}")
    else:
        print(f"skip: {PROMPT_SCOPE} missing")
    _append(METHODS, TAKE_METHOD, f"{MARKER} (take)", ("@method(\"vault.remove\")", "_profile_scoped"), "vault.take method")
    _append(REQUEST_CONTRACTS, REQUEST_CONTRACT, f"{MARKER} (request contract)",
            ("class ServerRequestParams", "server_request("), "capture_secret request contract")
    _append(METHOD_CONTRACTS, METHOD_CONTRACT, f"{MARKER} (method contract)",
            ("method(\"vault.remove\"", "ProfileParams"), "vault.take contract")


if __name__ == "__main__":
    main()
