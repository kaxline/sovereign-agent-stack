# Vendored: gptr-mcp

| | |
|---|---|
| Upstream | https://github.com/assafelovic/gptr-mcp |
| Commit | `6388477` (`master` when vendored on 2026-08-11 in 46c70ea; still `master` on 2026-11-11, last upstream change 2025-11-07) |
| License | MIT, copyright 2025 Assaf Elovic (upstream `LICENSE`, not copied here) |

The commit was found by diffing against upstream history, not recorded at the time: the
`Dockerfile` header says only "master". `server.py` and `utils.py` match `6291855` through
`6388477` once formatting and docstrings are ignored; the `/health` route and the curl
healthcheck place the copy at `6291855` or later, and no later commit touches those files.

docker-compose.yml builds this directory as the `gptr-mcp` service (profiles `research` and
`automation`) and serves MCP over SSE at `http://gptr-mcp:8000/sse`. `addons/gptr/addon.json`
registers its five tools with Hermes.

## Local changes

Commit 46c70ea, which added the copy, gives no reason for any of these. Where a reason is
given below it comes from the change itself.

- Only `server.py`, `utils.py`, `requirements.txt` and `Dockerfile` are kept. Upstream's
  `README.md`, `LICENSE`, `__init__.py`, `docker-compose.yml`, `run.sh`, `.env.example` and
  `tests/` are not here.
- `server.py`: reformatted (black-style wrapping, shorter docstrings), with three changes in
  behaviour. The startup, error and shutdown `print()` lines lose their emoji. The line
  "Check researcher_mcp_server.log for details" is removed; upstream never writes that file
  (`utils.py` logs to stderr only). `load_dotenv()` moves below the imports, and the unused
  `sys` and `List` imports are dropped. Tools, resources, the `/health` route and transport
  selection are unchanged.
- `utils.py`: reformatted, with docstring `Args:`/`Returns:` sections removed. The one change
  in output is `create_research_prompt()`, whose prompt text loses its leading indentation
  (same words, no leading spaces on each line).
- `requirements.txt`: upstream's test-only `httpx` and `sseclient-py` are dropped, since
  `tests/` isn't copied. The runtime lines are upstream's, including the unpinned
  `gpt-researcher>=0.14.0` and `fastmcp>=2.8.0`.
- `Dockerfile`: copies `server.py utils.py` instead of `COPY . .`; installs `curl` without
  `git` and with `--no-install-recommends` (c80b16b, for hadolint); healthcheck
  `--start-period=40s` instead of `7s`, matching the compose healthcheck.

None of these are worth filing upstream: they are trimming and formatting for this image.

## Updating

Upstream has no tags, so pick a commit on `master`. Copy its `server.py`, `utils.py` and
`requirements.txt` over this directory, keeping this file and the `Dockerfile`, then
reapply the trims above. Formatting need not be redone; take upstream's as-is unless it
breaks the hadolint or lint checks. Record the new commit in the table.

When upstream moves, or when a rebuild pulls a newer `gpt-researcher` or `fastmcp` (both are
unpinned, so any rebuild can):

- Check the tool names in `server.py` still match `mcp.tools` and `mcp.pin` in
  `addons/gptr/addon.json` (`deep_research`, `quick_search`, `write_report`,
  `get_research_sources`, `get_research_context`).
- Check the server still answers `GET /health` (compose healthcheck) and serves SSE at `/sse`
  when `MCP_TRANSPORT=sse`.
- Check whether upstream's `Dockerfile` now needs `git` or other system packages for a
  dependency.
- Run `scripts/test-gptr-mcp-registration.sh`. It covers registration only, not the server,
  so also rebuild and start the service and run one `quick_search` from a Hermes chat.
