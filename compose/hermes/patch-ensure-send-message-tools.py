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

from overlay_patch import ROOT, edit, one_of

TARGET = ROOT / "model_tools.py"
TAG = "[patch-ensure-send-message-tools]"
FORCE_MARKER = "# assistant-stack: force-import send_message_tool"

FORCE_OLD = """discover_builtin_tools()
"""

FORCE_NEW = """discover_builtin_tools()
# assistant-stack: force-import send_message_tool
# Bypass stale tool_discovery_cache registers=False entries so WebUI always
# gets the re-registered send_message tool.
# v0.23.2 moved it to gateway.outbound.send.
try:
    import tools.send_message_tool  # noqa: F401
except ImportError:
    try:
        import gateway.outbound.send  # noqa: F401
    except Exception:
        pass
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
                try:
                    import tools.send_message_tool  # noqa: F401
                except ImportError:
                    import gateway.outbound.send  # noqa: F401
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


# v0.23.2 filters the result through the hosted-tool allowlist before
# returning, so the block goes ahead of that filter: a hosted scope still
# drops send_message when its allowlist leaves it out.
ENSURE_OLD_HOSTED = """    except Exception as e:  # pragma: no cover — never break tool loading
        logger.warning("Tool search assembly skipped: %s", e)

    from tools.hosted_tool_policy import hosted_tool_refusal
"""

ENSURE_NEW_HOSTED = (
    """    except Exception as e:  # pragma: no cover — never break tool loading
        logger.warning("Tool search assembly skipped: %s", e)

"""
    + ENSURE_BLOCK
    + """    from tools.hosted_tool_policy import hosted_tool_refusal
"""
)


def main() -> None:
    # Both edits or neither, so FORCE_MARKER alone says whether the file is patched.
    edit(TARGET, [(FORCE_OLD, FORCE_NEW),
                  one_of((ENSURE_OLD, ENSURE_NEW), (ENSURE_OLD_HOSTED, ENSURE_NEW_HOSTED))],
         FORCE_MARKER, TAG, label=f"{TARGET} (force-import, ensure)")


if __name__ == "__main__":
    main()
