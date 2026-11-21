"""Per-turn prompt callbacks for the browser_vault actions the stack adds (stack-only).

Each action module (vault_create_login.py, vault_capture_secret.py) holds a
``PROMPT_SLOT`` and a ``make_prompt(ask)`` that builds its prompt from the
gateway's ``ask(method, params, timeout)``. gateway/session_vault_prompt.py's
prompt scope, patched once by whichever vault overlay runs first
(``PROMPT_SCOPE_EDITS`` below), calls ``install(ask)`` beside upstream's code
prompt and ``reset(tokens)`` when it restores it. A slot is a ContextVar, so
tool workers started with ``propagate_context_to_thread`` inherit it.

A new action adds its module to ``ACTION_MODULES``.
"""
from __future__ import annotations

import contextvars
import importlib
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

ACTION_MODULES = ("tools.vault_create_login", "tools.vault_capture_secret")


class PromptSlot:
    """One action's prompt callback for the current turn."""

    def __init__(self, name: str) -> None:
        self._var: contextvars.ContextVar[Optional[Callable[..., Any]]] = contextvars.ContextVar(
            f"assistant_stack_vault_{name}_prompt", default=None)

    def set(self, cb: Optional[Callable[..., Any]]) -> contextvars.Token:
        return self._var.set(cb)

    def reset(self, token: contextvars.Token) -> None:
        self._var.reset(token)

    def get(self) -> Optional[Callable[..., Any]]:
        return self._var.get()


def install(ask: Callable[[str, dict, float], Any]) -> list:
    """Set every installed action's prompt for this turn; returns the tokens for ``reset``."""
    tokens = []
    for name in ACTION_MODULES:
        try:
            module = importlib.import_module(name)
            tokens.append((module.PROMPT_SLOT, module.PROMPT_SLOT.set(module.make_prompt(ask))))
        except ImportError:
            continue  # that action's overlay is not installed
        except Exception:  # health: allow BLE001 -- the action then reports prompt_unavailable
            logger.warning("vault prompt slot for %s failed", name, exc_info=True)
    return tokens


def reset(tokens: list) -> None:
    for slot, token in reversed(tokens):
        slot.reset(token)


# The one edit both vault overlays make to gateway/session_vault_prompt.py's
# vault_prompt_scope; the marker makes the second a no-op.
PROMPT_SCOPE_MARKER = "# assistant-stack: vault prompt slots"
PROMPT_SCOPE_EDITS = [
    ("    def code_prompt(site, hint):\n",
     f"    {PROMPT_SCOPE_MARKER}\n"
     "    try:\n"
     "        from tools.vault_prompt_slot import install as _stack_install_slots, reset as _stack_reset_slots\n"
     "    except Exception:  # health: allow BLE001 -- stack vault actions then report prompt_unavailable\n"
     "        logger.debug(\"vault prompt slots unavailable\", exc_info=True)\n"
     "        _stack_install_slots = _stack_reset_slots = None\n"
     "\n"
     "    def code_prompt(site, hint):\n"),
    ("    unlock.set_code_prompt_callback(code_prompt)\n",
     "    _stack_slot_tokens = _stack_install_slots(ask) if _stack_install_slots else []\n"
     "    unlock.set_code_prompt_callback(code_prompt)\n"),
    ("        unlock.set_code_prompt_callback(previous[2])\n",
     "        if _stack_reset_slots is not None:\n"
     "            _stack_reset_slots(_stack_slot_tokens)\n"
     "        unlock.set_code_prompt_callback(previous[2])\n"),
]
