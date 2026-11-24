#!/usr/bin/env python3
"""Refuse unbuildable kanban workspaces at filing, give up on final workspace errors.

Compatibility overlay (not upstream), for Boundary change request 0006. Copies
kanban_guards.py into hermes_cli/ and:

- calls ``check_filing_workspace`` in ``create_task`` after the idempotency
  lookup (hermes_cli/kanban_db.py);
- passes ``force_trip`` and ``"retryable": false`` to ``_record_task_failure``
  when a spawn-time workspace error is final, and adds ``"retryable": false``
  to the terminal-provider give-up (hermes_cli/kanban_db_dispatch.py);
- installs the creator-comment wrap at the end of kanban_db_dispatch.py.

See kanban_guards.py for the design and docs/extending.md for the contract.
"""
from __future__ import annotations

from overlay_patch import BOOTSTRAP, ROOT, edit, install

MARKER = "# assistant-stack: kanban guards"
TAG = "[patch-kanban-guards]"


def _patch_create() -> None:
    anchor = "    # Only persistent kinds inherit the board ``default_workdir``: a scratch\n"
    check = (
        f"    {MARKER}: refuse a workspace the dispatcher could never build.\n"
        "    from hermes_cli.kanban_guards import check_filing_workspace as _assistant_check_workspace\n"
        "    _assistant_check_workspace(\n"
        "        workspace_kind=workspace_kind, workspace_path=workspace_path,\n"
        "        project_repo=project_repo, board=board,\n"
        "    )\n"
        "\n"
    )
    edit(ROOT / "hermes_cli" / "kanban_db.py", [(anchor, check + anchor)], MARKER, TAG,
         label="create_task workspace check")


def _patch_dispatch() -> None:
    spawn_old = (
        "    except Exception as exc:\n"
        "        if _record_task_failure(\n"
        "            conn, claimed.id, f\"workspace: {exc}\",\n"
        "            outcome=\"spawn_failed\", failure_limit=failure_limit, release_claim=True, end_run=True,\n"
        "        ):\n"
    )
    spawn_new = (
        "    except Exception as exc:\n"
        f"        {MARKER}: a workspace error that can't heal blocks on the first claim.\n"
        "        from hermes_cli.kanban_guards import workspace_failure_is_final as _assistant_ws_final\n"
        "        _ws_final = _assistant_ws_final(exc)\n"
        "        if _record_task_failure(\n"
        "            conn, claimed.id, f\"workspace: {exc}\",\n"
        "            outcome=\"spawn_failed\", failure_limit=failure_limit, release_claim=True, end_run=True,\n"
        "            force_trip=_ws_final, event_payload_extra={\"retryable\": False} if _ws_final else None,\n"
        "        ):\n"
    )
    provider_old = 'event_payload_extra={"pid": pid, "claimer": claimer, "terminal_provider": True},\n'
    provider_new = ('event_payload_extra={"pid": pid, "claimer": claimer, "terminal_provider": True,\n'
                    '                                     "retryable": False},\n')
    install_call = (
        f"\n{MARKER}: comment on the creator task when a task gives up.\n"
        "try:\n"
        "    from hermes_cli import kanban_guards as _assistant_kanban_guards\n"
        "    _assistant_kanban_guards.install_dispatch(sys.modules[__name__])\n"
        "except Exception:\n"
        "    import logging as _assistant_logging\n"
        "    _assistant_logging.getLogger(__name__).exception(\"kanban guards overlay failed to install\")\n"
    )
    edit(ROOT / "hermes_cli" / "kanban_db_dispatch.py", [(spawn_old, spawn_new), (provider_old, provider_new)],
         MARKER, TAG, label="dispatcher give-up", append=install_call)


def main() -> None:
    install(BOOTSTRAP / "kanban_guards.py", ROOT / "hermes_cli" / "kanban_guards.py", TAG)
    _patch_create()
    _patch_dispatch()


if __name__ == "__main__":
    main()
