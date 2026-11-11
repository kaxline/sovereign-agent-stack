# Vendored: lightrag-mcp

| | |
|---|---|
| Upstream | https://github.com/shemhamforash23/lightrag-mcp |
| Version | `lightrag_mcp==0.1.1` from PyPI (`LIGHTRAG_MCP_VERSION` build arg in `Dockerfile`) |
| Commit | `39ce48e` ("bump version to 0.1.1"); the 0.1.1 sdist's `src/` is identical to it |
| License | none declared (no `LICENSE` file, no `license` in `pyproject.toml`) |

No upstream source is copied here. The `Dockerfile` installs the PyPI release and patches two
files of the installed package in `site-packages` at build time. Upstream `master` is at
`0047c88` (2026-06-09), which adds an `--ssl` flag and is not released.

docker-compose.yml builds this directory as the `lightrag-mcp` service (profile `rag`),
serving streamable HTTP at `http://lightrag-mcp:8000/mcp` and calling LightRAG at
`lightrag:9621` with `--api-key ${LIGHTRAG_API_KEY}`. LightRAG is `ghcr.io/hkuds/lightrag:v1.5.7`
(`docker-compose.yml`). `addons/lightrag/addon.json` registers five of upstream's 17 tools:
`query_document`, `get_documents`, `get_pipeline_status`, `get_graph_labels`,
`check_lightrag_health`.

## Local changes

- `mcp>=1.6.0,<2` installed beside the package (fa11e33). Upstream requires `mcp>=1.6.0` with
  no upper bound, and its `server.py` imports `mcp.server.fastmcp`, which per the `Dockerfile`
  comment mcp 2.x dropped.
- `patch_lightrag_mcp.py`, auth header (fa11e33; moved out of a `RUN` heredoc in c80b16b with
  byte-identical output). `lightrag_client.py` builds
  `AuthenticatedClient(base_url=base_url, token=api_key, verify_ssl=False)`, and the generated
  client defaults to sending `Authorization: Bearer <key>`. LightRAG checks API keys only on
  `X-API-Key` and treats a bearer token as a login JWT, so every call gets a 401. The patch
  adds `prefix=""` and `auth_header_name="X-API-Key"`. Filing status: upstream issue
  [#15](https://github.com/shemhamforash23/lightrag-mcp/issues/15) (opened 2026-06-24 by
  someone else, same fix, no maintainer reply). A confirming comment and PR text are drafted for Keith in the
  gitignored `docs/private/lightrag-mcp-auth-header-upstream.md`.
- `patch_lightrag_mcp.py` plus `get_documents_paginated.py`, document list (7208633).
  LightRAG v1.5 answers `GET /documents` with 405, which broke the `get_documents` tool. The
  build appends `get_documents_paginated.py` to the generated
  `api/documents/documents_documents_get.py`, redefining its `asyncio()` to page through
  `POST /documents/paginated` (200 per page, LightRAG's maximum) and return the old
  `{"statuses": {status: [doc, ...]}}` shape. Documents stay plain dicts so v1.5's `parsing`
  and `analyzing` statuses, missing from the generated `DocStatus` enum, don't fail to parse;
  `error_msg` is copied to `error`, the pre-1.5 field name. Filing status: not filed.
  Upstream issue [#14](https://github.com/shemhamforash23/lightrag-mcp/issues/14) reports
  stale endpoints in general, and open PR
  [#13](https://github.com/shemhamforash23/lightrag-mcp/pull/13) fixes insert and delete but
  not the document list. It is a candidate for a filing once #15 gets a response.

Inserts are broken against v1.5 (`insert_document` gets a 400, per the CHANGELOG), but the
add-on never registers write tools, so no patch covers them. PR #13 targets the insert
request upstream.

## Updating

Change `LIGHTRAG_MCP_VERSION` in the `Dockerfile` and record the new version and commit in
the table. Then, and whenever the LightRAG image changes:

- The auth replace in `patch_lightrag_mcp.py` has no assert. If upstream changes the
  `AuthenticatedClient(...)` line, the replace does nothing, the build still passes, and every
  call gets a 401. If upstream fixes #15, delete that replace. Check the built image:

  ```bash
  docker compose --profile rag build lightrag-mcp
  ```

  ```bash
  docker compose --profile rag run --rm --no-deps --entrypoint grep lightrag-mcp -n X-API-Key /usr/local/lib/python3.11/site-packages/lightrag_mcp/lightrag_client.py
  ```

  No output means the patch did not apply.
- The document-list patch asserts that `documents_documents_get.py` still calls
  `"url": "/documents",`, so a renamed file or changed URL fails the build. Check that the
  redefined `asyncio()` signature still matches what `lightrag_client.get_documents` passes,
  and drop the patch if upstream moves to `/documents/paginated` itself.
- The `site-packages` path is hard-coded for `python:3.11-slim`; change it with the base image.
- Check whether the new release still needs `mcp<2`.
- If LightRAG changes, check `POST /documents/paginated` still takes `page`, `page_size`,
  `sort_field`, `sort_direction` and returns `documents` and `pagination.has_next` /
  `total_pages`.
- Run `scripts/test-lightrag-mcp-registration.sh`. It covers registration only, so also start
  the `rag` profile and call `get_documents` and `query_document` from a Hermes chat.
