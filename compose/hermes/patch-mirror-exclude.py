#!/usr/bin/env python3
"""``mirror_exclude`` on ``profiles.create``: keys a mirrored .env leaves out.

Compatibility overlay (not upstream yet). ``mirror_credentials`` (on by
default) copies the launch profile's whole .env into a new profile, minus
channel bot tokens. That includes every password saved for an MCP server: the
bootstrap's ``CALDAV_<SLUG>_PASSWORD`` keys and whatever a client stored with
``mcp.servers.set_api_key``, such as Boundary's ``BOUNDARY_CALENDAR_*``. An
agent that was not given a calendar should not hold its password.

``mirror_exclude`` is a list of glob patterns (``fnmatch``, case-sensitive)
matched against key names. Matching assignments are removed from the mirrored
.env after the channel strip; comments and other keys stay as they were.
``mirrored.env_excluded`` in the result lists the removed key names, never
values. If the filter fails, the new profile's .env is emptied and
``mirrored.env`` is false: no credentials rather than every credential.
Without ``mirror_exclude`` nothing changes.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
CONTRACT = ROOT / "tui_gateway" / "contracts" / "profiles_vault_complete_foreign_subagents.py"
METHODS = ROOT / "tui_gateway" / "methods_profiles.py"
MARKER = "# assistant-stack: mirror exclude"
TAG = "[patch-mirror-exclude]"

PARAMS_OLD = "    mirror_credentials: bool | str | None = None\n"
PARAMS_NEW = f"""    mirror_credentials: bool | str | None = None
    mirror_exclude: list[str] | None = None  {MARKER}
"""

RESULT_OLD = """    model_inherited: bool = False
    voice: bool = False


class ProfilesCreateResult(Result):
"""
RESULT_NEW = f"""    model_inherited: bool = False
    voice: bool = False
    env_excluded: list[str] | None = None  {MARKER}


class ProfilesCreateResult(Result):
"""

CALL_OLD = """        _best_effort(lambda: _lazy("hermes_cli.profile_channels", "strip_channel_env_file")(path / ".env"))
    if not share_auth:"""
CALL_NEW = f"""        _best_effort(lambda: _lazy("hermes_cli.profile_channels", "strip_channel_env_file")(path / ".env"))
    if mirrored["env"] and params.get("mirror_exclude"):  {MARKER}
        _assistant_mirror_exclude(path / ".env", params["mirror_exclude"], mirrored)
    if not share_auth:"""

HELPER_ANCHOR = "def _mirror_launch_credentials(path, params: dict) -> dict:\n"
HELPER = f'''def _assistant_mirror_exclude(env_path, patterns, mirrored: dict) -> None:  {MARKER}
    """Drop .env assignments whose key matches a ``mirror_exclude`` glob; fail closed."""
    import fnmatch
    import re
    try:
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list) or not all(isinstance(p, str) and p for p in patterns):
            raise ValueError("mirror_exclude must be a list of non-empty strings")
        key_re = re.compile(r"^\\s*(?:export\\s+)?([A-Za-z_][A-Za-z0-9_]*)\\s*=")
        text = env_path.read_text(encoding="utf-8-sig", errors="replace")
        kept, removed = [], []
        for line in text.splitlines():
            match = key_re.match(line)
            if match and any(fnmatch.fnmatchcase(match.group(1), p) for p in patterns):
                removed.append(match.group(1))
            else:
                kept.append(line)
        if removed:
            env_path.write_text("\\n".join(kept) + ("\\n" if kept else ""), encoding="utf-8")
        mirrored["env_excluded"] = sorted(set(removed))
    except Exception as exc:
        logging.getLogger(__name__).warning("mirror_exclude failed (%s); emptying the mirrored .env", exc)
        with contextlib.suppress(OSError):
            env_path.write_text("", encoding="utf-8")
        mirrored["env"] = False
        mirrored["env_excluded"] = None


'''


def patch(path: Path, edits: list[tuple[str, str, str]]) -> None:
    text = path.read_text()
    if MARKER in text:
        print(f"{TAG} already applied ({path.name})")
        return
    for old, _, label in edits:
        if text.count(old) != 1:
            raise SystemExit(f"{TAG} expected 1 x {label} in {path}, found {text.count(old)}. "
                             "Hermes image may have changed — update this patch.")
    for old, new, _ in edits:
        text = text.replace(old, new)
    path.write_text(text)
    print(f"{TAG} patched {path.name}")


def main() -> None:
    patch(CONTRACT, [(PARAMS_OLD, PARAMS_NEW, "mirror_credentials param"),
                     (RESULT_OLD, RESULT_NEW, "ProfileMirrored fields")])
    methods = METHODS.read_text()
    for needed in ("\nimport contextlib\n", "\nimport logging\n"):
        if needed not in methods and MARKER not in methods:
            raise SystemExit(f"{TAG} {needed.strip()!r} missing in {METHODS}. Hermes image may have changed — update this patch.")
    patch(METHODS, [(CALL_OLD, CALL_NEW, "channel strip call"),
                    (HELPER_ANCHOR, HELPER + HELPER_ANCHOR, "_mirror_launch_credentials")])


if __name__ == "__main__":
    main()
