#!/usr/bin/env python3
"""Ensure send_message survives into get_tool_definitions output.

Stale tool_discovery_cache entries can skip importing send_message_tool for
the life of a gateway process. Force-import at module load helps, but reinject
here if the tool still dropped out of the assembled schema.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("/opt/hermes/model_tools.py")
MARKER = "# assistant-stack: ensure send_message in tool defs"

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

OLD = """    except Exception as e:  # pragma: no cover — never break tool loading
        logger.warning("Tool search assembly skipped: %s", e)

    return filtered_tools
"""

NEW = (
    """    except Exception as e:  # pragma: no cover — never break tool loading
        logger.warning("Tool search assembly skipped: %s", e)

"""
    + ENSURE_BLOCK
    + """    return filtered_tools
"""
)


def main() -> int:
    if not TARGET.is_file():
        print(f"skip: missing {TARGET}")
        return 0
    text = TARGET.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"already patched: {TARGET}")
        return 0
    if OLD not in text:
        print(f"skip: expected snippet not found in {TARGET}")
        return 1
    TARGET.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print(f"patched: {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
