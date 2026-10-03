# Security Policy

## Supported versions

This is a **local development** stack. It is not hardened for internet-facing deployment.

| Component | Exposure | Auth |
|---|---|---|
| Hermes dashboard | `127.0.0.1:9119` | HTTP basic auth (`HERMES_DASHBOARD_PASSWORD`) |
| Hermes WebUI | `127.0.0.1:8787` | Relies on loopback bind + gateway API key |
| Hermes API | `127.0.0.1:8643` | Bearer token (`compose/hermes/api-server.env`) |
| Hermes browser gateway | compose-internal `:8644` | Bearer token (`compose/hermes/browser.env`) |
| SearXNG | `127.0.0.1:8080` | Shared secret (`searxng/settings.local.yml`) |
| LightRAG | `127.0.0.1:9621` | API key (`LIGHTRAG_API_KEY`) |
| Neo4j | `127.0.0.1:7474` (Bolt `127.0.0.1:7687`) | Username/password |
| n8n | `127.0.0.1:5678` | Owner account (first login) |
| OpenCode | `127.0.0.1:4096` | HTTP basic auth |
| opencode-mcp (optional) | compose-internal `:8000` | None — keep unpublished; Hermes-only |
| GPT Researcher | `127.0.0.1:8000` | None (loopback-only) |
| llm-proxy inference | compose-internal `:4000` | Placeholder `local-llm`; real keys stay on this service |
| llm-proxy admin | `127.0.0.1:4001` | Bearer `LLM_PROXY_ADMIN_TOKEN`; updates stay in memory |
| caldav-mcp (optional) | compose-internal `:8080` | Per-request CalDAV credentials from Hermes MCP headers; no host publish |
| signal-cli (optional) | compose-internal `:8080` | None — HTTP JSON-RPC has no auth; keep unpublished |

## Reporting vulnerabilities

If you discover a security issue in this repository, please open a private advisory or contact the maintainers directly. Do not open public issues for undisclosed vulnerabilities.

## Local-dev defaults

`./scripts/setup.sh` generates random passwords and API keys. Treat them as secrets on your machine.

**Do not:**

- Expose n8n, SearXNG, GPT Researcher, or Hermes to the public internet without authentication and rate limiting.
- Commit `.env`, `compose/hermes/api-server.env`, `compose/hermes/browser.env`, `compose/caldav-mcp/accounts/*.env`, `n8n/demo-data/credentials/*.json`, `searxng/settings.local.yml`, `opencode/opencode.local.json`, `docker-compose.override.yml`, or anything under `data/`.
- Reuse one `API_SERVER_KEY` across Hermes profiles. Keys are scoped per profile and a shared key fails closed, so give `compose/hermes/api-server.env` and `compose/hermes/browser.env` distinct values.
- Point `OPENCODE_WORKSPACE_HOST` at `$HOME` or `/` — OpenCode and Hermes have full read/write access to coding roots.
- Use `make coding-root-add DIR=...` to grant broad parents lightly; prefer the specific repo path. Never add `/` or `$HOME` alone (the script refuses those).
- Use `make context-root-add DIR=...` the same way: read-only notes mounts still expose everything under that path to Hermes. Never add `/` or `$HOME` alone.
- Publish the signal-cli HTTP port to the LAN. The daemon has no authentication; anyone who can reach it can send as your linked Signal account. This stack keeps it compose-internal only.
- Publish the caldav-mcp HTTP port. Calendar credentials travel as MCP headers from Hermes; keep the sidecar compose-internal.

**Do:**

- Keep the root `.env` on the host. Compose interpolates `${VAR}` from it and does not mount the file into containers. Each service lists the environment keys it reads. A new secret in `.env` stays out of every container until that service's list includes it. The Hermes dashboard password remains in the `hermes` process environment because the dashboard and the agent share that container.
- Run Hermes shell and file tools on `hermes-worker` (`HERMES_TERMINAL_BACKEND=ssh`, the default). That container has no secrets, no host port, and no `data/hermes` mount. The SSH private key lives only on `hermes`, outside `/opt/data`. `terminal.env_passthrough` is unset and the worker ignores forwarded environment. `/opt/projects` stays read-only on `hermes` so the in-process project library can list it; writes go to the worker. `bot_desktop.placement` is `gateway`, so the browser stays in `hermes`. Move leftover coding or context binds in `docker-compose.override.yml` from `hermes` to `hermes-worker` (`make ensure-local` does this for roots listed in `.env`). See [docs/hermes.md](docs/hermes.md).
- Treat `HERMES_TERMINAL_BACKEND=local` as an escape hatch for debugging or a machine that cannot run the extra container. It does not start the worker, and it mounts the workspace read-write on `hermes`. That container also holds the dashboard password and profile secrets, so the shell is no longer isolated from them. Switch back to `ssh` when you are done.
- Send model calls through `llm-proxy`. LightRAG, GPT Researcher, OpenCode, and Hermes use the placeholder `local-llm`. `LLM_BINDING_API_KEY`, `LIGHTRAG_LLM_BINDING_API_KEY`, and `EMBEDDING_BINDING_API_KEY` are interpolated only into the proxy. The inference port is not published. The admin API is on `127.0.0.1` and keeps pushed keys in memory only.
- Publish host ports on `127.0.0.1` only. LightRAG, Neo4j, n8n, and OpenCode are bound that way, same as Hermes, the WebUI, SearXNG, GPT Researcher, and Ollama. A LAN client needs an SSH tunnel.
- Shadow every secret under `OPENCODE_WORKSPACE_HOST` (and any `CODING_EXTRA_ROOTS`) if you mount a parent folder of many projects. A parent mount exposes each project's `.env` to the agent; mounting `compose/opencode/blank` read-only over each one leaves it reading an empty file and unable to overwrite the real one. Keep those machine-specific mounts in `docker-compose.override.yml` (gitignored). See [docs/opencode.md](docs/opencode.md).
- Re-run setup or rotate secrets if you suspect leakage.
- Set a strong `HERMES_DASHBOARD_PASSWORD`. As of agent 0.20.0 the dashboard will not bind a non-loopback interface without an auth provider, so basic auth has to be there for the healthcheck to pass. Move to OAuth (`hermes dashboard register`) if Hermes is reachable beyond localhost.
- Keep the Hermes LightRAG MCP allowlist as a security boundary for unattended API sessions (five read-oriented tools). The bootstrap drops unfiltered clone duplicates so nothing routes around it.
- Keep the Hermes CalDAV MCP allowlist as a security boundary for unattended API sessions (eight read-oriented tools). Account passwords live in gitignored `compose/caldav-mcp/accounts/*.env`. Bootstrap writes `${CALDAV_<SLUG>_PASSWORD}` into the MCP headers and the secret into each profile's `.env` under `data/hermes/`. The password is still in that tree — treat it like a password store — but config dumps no longer include it. See [docs/calendar.md](docs/calendar.md).
- Keep OpenCode / `coding_*` MCP **off** the Hermes `api-server` profile. Coding tools mutate host repos; they are registered only on default + browser when `OPENCODE_MCP_ENABLED=1`.
- Enable SearXNG rate limiting (`--profile searxng-prod`) if the instance is shared on a LAN.
- Treat `SIGNAL_CLI_DATA_DIR` (linked session data) like a password, and keep `SIGNAL_ALLOWED_USERS` tight when Signal is enabled.

## Dependency updates

Container images are pinned to version tags in `docker-compose.yml` and `.env.example`. Bump deliberately, re-run `make doctor`, and monitor upstream CVEs for production-like deployments.
