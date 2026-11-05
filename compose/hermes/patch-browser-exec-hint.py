#!/usr/bin/env python3
"""Fix the hints browser_exec gives the model.

- A result gains a ``hint`` when the code imported the pre-defined helpers
  from a module that does not exist, or ran and printed nothing.
- A trailing bare expression (``js(...)``, ``page_info()``) is printed, as a
  Python prompt does, so its value comes back.
- browser_vault_save_login's ``next`` names browser_vault_fill and the handle
  when the save could not fill the page (no login form open).

The logic lives in browser_exec_hints.py, copied into /opt/hermes/tools/.
"""
from __future__ import annotations

import shutil
from pathlib import Path

TARGET = Path("/opt/hermes/tools/browser_use_cli.py")
HELPER_SRC = Path("/bootstrap/browser_exec_hints.py")
HELPER_DST = Path("/opt/hermes/tools/browser_exec_hints.py")
MARKER_RESULT = "# assistant-stack: browser_exec result hints"
MARKER_ECHO = "# assistant-stack: print a trailing browser_exec expression"
VAULT_TARGET = Path("/opt/hermes/tools/browser_vault_tool.py")
MARKER_SAVE_NEXT = "# assistant-stack: vault save next step"

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

OLD_ECHO = (
    "    timeout = _clamp_timeout(timeout_s)\n"
    "    started = time.time()\n"
)

NEW_ECHO = (
    "    # assistant-stack: print a trailing browser_exec expression\n"
    "    try:\n"
    "        from tools.browser_exec_hints import echo_last_expression\n"
    "        code = echo_last_expression(code)\n"
    "    except Exception:\n"
    "        logger.debug(\"browser_exec echo failed\", exc_info=True)\n"
    "    timeout = _clamp_timeout(timeout_s)\n"
    "    started = time.time()\n"
)

OLD_SAVE_NEXT = (
    "    filled = json.loads(browser_vault_fill(meta.id, task_id=effective_task_id))\n"
    "    return json.dumps({\"success\": True, \"handle\": meta.id, \"origin\": origin, \"identifier\": identifier,\n"
    "                       \"identifier_type\": id_type, \"fill\": filled,\n"
    "                       \"next\": \"Type the identifier into the username field if the form has one, then submit.\"},\n"
)

NEW_SAVE_NEXT = (
    "    filled = json.loads(browser_vault_fill(meta.id, task_id=effective_task_id))\n"
    "    # assistant-stack: vault save next step\n"
    "    _save_next = \"Type the identifier into the username field if the form has one, then submit.\"\n"
    "    try:\n"
    "        from tools.browser_exec_hints import save_login_next\n"
    "        _save_next = save_login_next(meta.id, bool(filled.get(\"success\")))\n"
    "    except Exception:\n"
    "        logger.debug(\"vault save next-step hint failed\", exc_info=True)\n"
    "    return json.dumps({\"success\": True, \"handle\": meta.id, \"origin\": origin, \"identifier\": identifier,\n"
    "                       \"identifier_type\": id_type, \"fill\": filled,\n"
    "                       \"next\": _save_next},\n"
)


def _apply(text: str, old: str, new: str, marker: str, label: str, path: Path = TARGET) -> str:
    if marker in text:
        print(f"ok: {label} already patched")
        return text
    if old not in text:
        print(f"warn: {label} needle missing in {path}")
        return text
    print(f"patched: {label} in {path}")
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
    patched = _apply(text, OLD_RESULT, NEW_RESULT, MARKER_RESULT, "result hints")
    patched = _apply(patched, OLD_ECHO, NEW_ECHO, MARKER_ECHO, "expression echo")
    if patched != text:
        TARGET.write_text(patched)
    if VAULT_TARGET.exists():
        vault = VAULT_TARGET.read_text()
        vault_patched = _apply(vault, OLD_SAVE_NEXT, NEW_SAVE_NEXT, MARKER_SAVE_NEXT,
                               "vault save next step", VAULT_TARGET)
        if vault_patched != vault:
            VAULT_TARGET.write_text(vault_patched)
    else:
        print(f"skip: {VAULT_TARGET} missing")


if __name__ == "__main__":
    main()
