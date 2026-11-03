#!/usr/bin/env python3
"""Let a client wake a parked MCP server, and tune the parked probe per server.

Compatibility overlay (not upstream). Copies mcp_wake.py into tools/ and:

- reads ``mcp_servers.<name>.parked_retry_interval`` when a server parks
  (tools/mcp_tool_server_run.py), and marks the task parked while it waits;
- parks a server after one failed initial attempt when another profile has
  just parked it after the full initial ladder (same file);
- starts the wake watcher with the MCP loop (tools/mcp_tool_loop.py);
- adds ``POST /v1/mcp/servers/{name}/reconnect`` to the API server
  (gateway/platforms/api_server.py).

See mcp_wake.py for the design and docs/extending.md for the contract.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
BOOTSTRAP = Path(os.environ.get("MCP_WAKE_BOOTSTRAP", "/bootstrap"))
MARKER = "# assistant-stack: mcp wake"
TAG = "[patch-mcp-wake]"


def _install() -> None:
    src, dst = BOOTSTRAP / "mcp_wake.py", ROOT / "tools" / "mcp_wake.py"
    if not src.is_file():
        raise SystemExit(f"{TAG} missing {src}")
    shutil.copyfile(src, dst)
    print(f"{TAG} installed {dst}")


def _edit(path: Path, label: str, edits: list[tuple[str, str, int]]) -> None:
    """Apply (old, new, expected_count) edits to *path* once; MARKER makes it idempotent."""
    text = path.read_text()
    if MARKER in text:
        print(f"{TAG} already applied ({label})")
        return
    for old, new, count in edits:
        found = text.count(old)
        if found != count:
            raise SystemExit(
                f"{TAG} expected {count} x {old.strip()[:60]!r} in {path}, found {found}. "
                "Hermes image may have changed — update this patch.")
        text = text.replace(old, new)
    path.write_text(text)
    print(f"{TAG} patched {label}")


_INTERVAL = "_assistant_mcp_wake.parked_retry_interval(self._config, _core._PARKED_RETRY_INTERVAL)"


def _patch_server_run() -> None:
    # MCPServerRunMixin._park: the timed wait inside its ``while True`` loop.
    wait = ("            outcome = await self._wait_for_reconnect_or_shutdown(\n"
            "                timeout=_core._PARKED_RETRY_INTERVAL)\n")
    ladder = ("        budget.initial_retries += 1\n"
              "        if budget.initial_retries > _core._MAX_INITIAL_CONNECT_RETRIES:\n")
    _edit(ROOT / "tools" / "mcp_tool_server_run.py", "parked retry interval", [
        ("from tools import mcp_tool_sampling as _sampling\n",
         "from tools import mcp_tool_sampling as _sampling\n"
         f"from tools import mcp_wake as _assistant_mcp_wake  {MARKER}\n", 1),
        # The wait itself: per-server interval, parked flag for the wake watcher.
        (wait,
         f"            self._assistant_parked = True  {MARKER}\n"
         "            try:\n"
         "                outcome = await self._wait_for_reconnect_or_shutdown(\n"
         f"                    timeout={_INTERVAL})\n"
         "            finally:\n"
         "                self._assistant_parked = False\n", 1),
        # The three "will self-probe every %ds" warnings.
        ("_core._PARKED_RETRY_INTERVAL, type(root).__name__, root)",
         f"{_INTERVAL}, type(root).__name__, root)", 2),
        ("self.name, _core._MAX_RECONNECT_RETRIES, _core._PARKED_RETRY_INTERVAL)\n",
         f"self.name, _core._MAX_RECONNECT_RETRIES, {_INTERVAL})\n", 1),
        # _on_initial_connect_error: skip the ladder when another profile just ran it.
        (ladder,
         "        if budget.initial_retries == 0 and _assistant_mcp_wake.peer_parked(\n"
         f"                self.name, self._config):  {MARKER}\n"
         "            self._log_park(\n"
         "                \"MCP server '%s' failed initial connection and another profile parked it after \"\n"
         "                \"its retries, parking without retries (state: connecting → parked): %s: %s\",\n"
         "                self.name, type(root).__name__, root)\n"
         "            return await self._park_initial_failure(exc, \"after initial connection failures\", budget)\n"
         + ladder
         + "            _assistant_mcp_wake.note_initial_park(self.name, self._config)\n", 1),
    ])


def _patch_loop() -> None:
    start = "        _origin._mcp_thread.start()\n"
    _edit(ROOT / "tools" / "mcp_tool_loop.py", "wake watcher", [
        (start,
         start
         + f"        from tools import mcp_wake as _assistant_mcp_wake  {MARKER}\n"
         + "        _assistant_mcp_wake.start_watcher()\n", 1),
    ])


def _patch_api_server() -> None:
    route = '            ("GET", "/v1/toolsets", self._handle_toolsets),\n'
    table = "    def _http_route_table(self) -> list[tuple]:\n"
    handler = (
        f"    @_require_auth  {MARKER}\n"
        '    async def _handle_mcp_reconnect(self, request: "web.Request") -> "web.Response":\n'
        '        """POST /v1/mcp/servers/{name}/reconnect: wake a parked MCP server for every profile\n'
        '        and process (202), or 404 when no profile configures it. See tools/mcp_wake.py."""\n'
        "        from tools import mcp_wake\n"
        '        body, status = mcp_wake.handle_reconnect(request.match_info.get("name", ""))\n'
        "        return web.json_response(body, status=status)\n\n"
    )
    _edit(ROOT / "gateway" / "platforms" / "api_server.py", "reconnect endpoint", [
        (route,
         route + f'            ("POST", "/v1/mcp/servers/{{name}}/reconnect", self._handle_mcp_reconnect),  {MARKER}\n',
         1),
        (table, handler + table, 1),
    ])


def main() -> None:
    _install()
    _patch_server_run()
    _patch_loop()
    _patch_api_server()


if __name__ == "__main__":
    main()
