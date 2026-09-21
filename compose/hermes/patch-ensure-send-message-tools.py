#!/usr/bin/env python3
"""Keep send_message visible in model_tools despite stale discovery cache.

1. Force-import send_message_tool after discover_builtin_tools() so a cached
   registers=False entry cannot skip the module for the life of the process.
2. Reinject send_message into get_tool_definitions output if it still dropped
   out of the assembled schema.

Formerly patch-force-import-send-message.py + patch-ensure-send-message-tools.py.
cont-init also clears tool_discovery_cache.json after these apply.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("/opt/hermes/model_tools.py")
FORCE_MARKER = "# assistant-stack: force-import send_message_tool"
ENSURE_MARKER = "# assistant-stack: ensure send_message in tool defs"

FORCE_OLD = """discover_builtin_tools()
"""

FORCE_NEW = """discover_builtin_tools()
# assistant-stack: force-import send_message_tool
# Bypass stale tool_discovery_cache registers=False entries so WebUI always
# gets the re-registered send_message tool.
try:
    import tools.send_message_tool  # noqa: F401
except Exception:
    pass
"""

ENSURE_BLOCK = """    # assistant-stack: ensure send_message in tool defs
    try:
        _names = {
            (td.get("function") or {}).get("name")
            for td in filtered_tools
            if isinstance(td, dict)
        }
        if "send_message" not in _names:
            try:
                import tools.send_message_tool  # noqa: F401
            except Exception as _imp_err:
                logger.warning("assistant-stack send_message import failed: %s", _imp_err)
            _extra = registry.get_definitions({"send_message"}, quiet=True) or []
            if _extra:
                filtered_tools = list(filtered_tools) + list(_extra)
    except Exception as _ens_err:
        logger.warning("assistant-stack send_message ensure failed: %s", _ens_err)

"""

ENSURE_OLD = """    except Exception as e:  # pragma: no cover — never break tool loading
        logger.warning("Tool search assembly skipped: %s", e)

    return filtered_tools
"""

ENSURE_NEW = (
    """    except Exception as e:  # pragma: no cover — never break tool loading
        logger.warning("Tool search assembly skipped: %s", e)

"""
    + ENSURE_BLOCK
    + """    return filtered_tools
"""
)


def _patch_force_import(text: str) -> tuple[str, str]:
    if FORCE_MARKER in text:
        return text, f"already patched (force-import): {TARGET}"
    if FORCE_OLD not in text:
        return text, f"skip: force-import snippet not found in {TARGET}"
    return text.replace(FORCE_OLD, FORCE_NEW, 1), f"patched (force-import): {TARGET}"


def _patch_ensure(text: str) -> tuple[str, str]:
    if ENSURE_MARKER in text:
        return text, f"already patched (ensure): {TARGET}"
    if ENSURE_OLD not in text:
        return text, f"skip: ensure snippet not found in {TARGET}"
    return text.replace(ENSURE_OLD, ENSURE_NEW, 1), f"patched (ensure): {TARGET}"


def main() -> int:
    if not TARGET.is_file():
        print(f"skip: missing {TARGET}")
        return 0
    text = TARGET.read_text(encoding="utf-8")
    text, msg_force = _patch_force_import(text)
    print(msg_force)
    text, msg_ensure = _patch_ensure(text)
    print(msg_ensure)
    if msg_force.startswith("patched") or msg_ensure.startswith("patched"):
        TARGET.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
