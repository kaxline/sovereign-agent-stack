#!/usr/bin/env python3
"""browser_vault action=create_login (CR 0024 R1): a sign-up login with a generated password.

- tools/browser_vault_tool.py: the schema gains the action and an ``identifier`` argument, and the
  dispatcher routes it to tools/vault_create_login.py (copied from /bootstrap).
- gateway/session_vault_prompt.py: a local viewer's turn also installs the create_login prompt, which
  sends the ``vault.create_login`` server request ``{origin, site, identifier, regenerate}``.
- tui_gateway/contracts/server_requests.py: the request's contract, so it is advertised in the
  ``server_requests`` list a client reads at connect time.

Idempotent; each part reports ok / patched / warn on its own.
"""
from __future__ import annotations

import shutil
from pathlib import Path

HERMES = Path("/opt/hermes")
HELPER_SRC = Path("/bootstrap/vault_create_login.py")
HELPER_DST = HERMES / "tools" / "vault_create_login.py"
VAULT_TOOL = HERMES / "tools" / "browser_vault_tool.py"
PROMPT_SCOPE = HERMES / "gateway" / "session_vault_prompt.py"
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

OLD_CODE_PROMPT = (
    "    def code_prompt(site, hint):\n"
    "        return ask(\"vault.code\", {\"site\": site, \"hint\": hint}, CODE_TIMEOUT_S)\n"
)
NEW_CODE_PROMPT = OLD_CODE_PROMPT + (
    "\n"
    "    # assistant-stack: vault create_login (prompt)\n"
    "    def create_login_prompt(origin, site, identifier, regenerate=False):\n"
    "        from tools.vault_create_login import PROMPT_METHOD, PROMPT_TIMEOUT_S, parse_answer\n"
    "        return parse_answer(ask(PROMPT_METHOD, {\"origin\": origin, \"site\": site, \"identifier\": identifier,\n"
    "                                               \"regenerate\": bool(regenerate)}, PROMPT_TIMEOUT_S))\n"
    "\n"
    "    try:\n"
    "        from tools.vault_create_login import reset_prompt_callback as _stack_reset_create_login\n"
    "        from tools.vault_create_login import set_prompt_callback as _stack_set_create_login\n"
    "    except Exception:  # health: allow BLE001 -- create_login then reports prompt_unavailable\n"
    "        logger.debug(\"vault create_login prompt overlay unavailable\", exc_info=True)\n"
    "        _stack_set_create_login = _stack_reset_create_login = None\n"
)

OLD_INSTALL = "    unlock.set_code_prompt_callback(code_prompt)\n"
NEW_INSTALL = OLD_INSTALL + (
    "    _stack_create_token = _stack_set_create_login(create_login_prompt) if _stack_set_create_login else None\n"
)

OLD_RESTORE = "        unlock.set_code_prompt_callback(previous[2])\n"
NEW_RESTORE = OLD_RESTORE + (
    "        if _stack_create_token is not None:\n"
    "            _stack_reset_create_login(_stack_create_token)\n"
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


def main() -> None:
    if not HELPER_SRC.is_file():
        print(f"warn: {HELPER_SRC} missing; create_login off")
        return
    shutil.copyfile(HELPER_SRC, HELPER_DST)
    print(f"installed: {HELPER_DST}")
    _patch_file(VAULT_TOOL, [
        (OLD_REGISTER, NEW_REGISTER, f"{MARKER} (schema)", "create_login schema"),
        (OLD_DISPATCH, NEW_DISPATCH, f"{MARKER} (dispatch)", "create_login dispatch"),
    ])
    # The prompt's three edits are one unit: install without restore would leak the callback.
    if PROMPT_SCOPE.exists():
        text = PROMPT_SCOPE.read_text()
        if f"{MARKER} (prompt)" in text:
            print("ok: create_login prompt already patched")
        elif all(old in text for old in (OLD_CODE_PROMPT, OLD_INSTALL, OLD_RESTORE)):
            text = (text.replace(OLD_CODE_PROMPT, NEW_CODE_PROMPT, 1).replace(OLD_INSTALL, NEW_INSTALL, 1)
                    .replace(OLD_RESTORE, NEW_RESTORE, 1))
            PROMPT_SCOPE.write_text(text)
            print(f"patched: create_login prompt in {PROMPT_SCOPE}")
        else:
            print(f"warn: create_login prompt needles missing in {PROMPT_SCOPE}")
    else:
        print(f"skip: {PROMPT_SCOPE} missing")
    if CONTRACTS.exists():
        text = CONTRACTS.read_text()
        if f"{MARKER} (contract)" in text:
            print("ok: create_login contract already patched")
        elif "class ServerRequestParams" in text and "server_request(" in text:
            CONTRACTS.write_text(text.rstrip("\n") + "\n" + CONTRACT)
            print(f"patched: create_login contract in {CONTRACTS}")
        else:
            print(f"warn: create_login contract anchors missing in {CONTRACTS}")
    else:
        print(f"skip: {CONTRACTS} missing")


if __name__ == "__main__":
    main()
