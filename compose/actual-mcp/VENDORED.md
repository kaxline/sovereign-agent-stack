# Vendored: actual-mcp

| | |
|---|---|
| Upstream | https://github.com/giorgiobrullo/actual-mcp (a fork of https://github.com/s-stefanov/actual-mcp) |
| Commit | `0c73b0d727a1d32bb924ab323985fffc9f0944fb` (`version` 1.15.1 in `package.json`) |
| License | MIT, copyright 2025 Stefan Stefanov (`LICENSE`) |

docker-compose.yml builds this directory as `${IMAGE_PREFIX:-assistant}-actual-mcp:local` and runs it
twice: `actual-mcp-rw` with `--sse --enable-write` and `actual-mcp-ro` with `--sse`.

## Local changes

- `package.json`: `@actual-app/api` and `@actual-app/core` pointed at
  `file:../actual/packages/api` and `file:../actual/packages/loot-core` (the upstream author's
  local Actual checkout), so the repo did not build on its own. Both are now `26.11.0` from npm,
  the version of the pinned `actual-server` image.
- `package-lock.json`: regenerated against those versions with
  `npm install --package-lock-only --ignore-scripts` in `node:24-alpine` (node 24.22.2, npm 11.19.1).
- `Dockerfile`: the release stage creates `/data` owned by `node` and switches to `USER node`.
  Upstream runs as root. `/data` is the budget cache (`ACTUAL_DATA_DIR`).
- `src/integrations/actual/connection.ts`: `syncIfStale()` no longer answers from the loaded copy
  when a sync fails because the session ended. When `api.sync()` throws with `code`
  `token-expired`, `token-not-found`, `unauthorized`, `invalid-password`,
  `file-access-not-allowed` or `budget-not-found`, it shuts the API down, marks the connection
  failed and fails the call with `Actual session ended (<code>): reconnect with make budget-add
  FORCE=1 or make budget-init`; the next call re-runs init. Other sync errors, network ones
  included, are logged and the call continues with local data, as upstream does. Tests in
  `connection.test.ts` and `actual-api.test.ts`. Upstream logs and continues on every sync error,
  so after a password change or a deleted session, writes were reported as done and stayed in
  the local cache.
- `actual-admin.mjs` (local addition, not from upstream): the `check`, `create` and `list` commands
  `scripts/budget.sh` runs with `--entrypoint node`. The Dockerfile copies it to
  `/opt/actual-admin.mjs`; it loads `@actual-app/api` from `/app/node_modules`.
- Removed, as unused by the build or the image: `.github/`, `.vscode/`, `docs/`, `e2e/`,
  `vitest.e2e.config.ts`, `AGENTS.md`, `CLAUDE.md`, `CHANGELOG.md`, `cicd.md`,
  `release-please-config.json`, `.release-please-manifest.json`, `renovate.json`.

Apart from `connection.ts` above, `src/` is unchanged. Upstream commit `0c73b0d` changed `run-bank-sync` to report the ids
returned by `runBankSync` in the author's Actual fork. npm `26.11.0` declares the same return
type (`Promise<SyncResponse>` in `@actual-app/api/@types/methods.d.ts`), so the build
type-checks with no patch. That tool has not been run. The spike ran `get-accounts` (rw and ro)
and `create-transaction` (rw).

## Updating

Copy the new upstream commit over this directory, keeping this file and `actual-admin.mjs`, then
reapply the changes above and regenerate the lockfile in a node container:

```bash
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD/compose/actual-mcp:/app" -w /app \
  node:24-alpine npm install --package-lock-only --ignore-scripts
```
