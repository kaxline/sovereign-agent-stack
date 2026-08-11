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
| LightRAG | `localhost:9621` | API key (`LIGHTRAG_API_KEY`) |
| Neo4j | `localhost:7474` | Username/password |
| n8n | `localhost:5678` | Owner account (first login) |
| OpenCode | `localhost:4096` | HTTP basic auth |
| GPT Researcher | `127.0.0.1:8000` | None (loopback-only) |

## Reporting vulnerabilities

If you discover a security issue in this repository, please open a private advisory or contact the maintainers directly. Do not open public issues for undisclosed vulnerabilities.

## Local-dev defaults

`./scripts/setup.sh` generates random passwords and API keys. Treat them as secrets on your machine.

**Do not:**

- Expose n8n, SearXNG, GPT Researcher, or Hermes to the public internet without authentication and rate limiting.
- Commit `.env`, `compose/hermes/api-server.env`, `compose/hermes/browser.env`, `n8n/demo-data/credentials/*.json`, `searxng/settings.local.yml`, `opencode/opencode.local.json`, `docker-compose.override.yml`, or anything under `data/`.
- Reuse one `API_SERVER_KEY` across Hermes profiles. Keys are scoped per profile and a shared key fails closed, so give `compose/hermes/api-server.env` and `compose/hermes/browser.env` distinct values.
- Point `OPENCODE_WORKSPACE_HOST` at `$HOME` or `/` — OpenCode has full read/write access to the mount.

**Do:**

- Shadow every secret under `OPENCODE_WORKSPACE_HOST` if you mount a parent folder of many projects. A parent mount exposes each project's `.env` to the agent; mounting `compose/opencode/blank` read-only over each one leaves it reading an empty file and unable to overwrite the real one. Keep those machine-specific mounts in `docker-compose.override.yml` (gitignored). See [docs/opencode.md](docs/opencode.md).
- Re-run setup or rotate secrets if you suspect leakage.
- Set a strong `HERMES_DASHBOARD_PASSWORD`. As of agent 0.20.0 the dashboard will not bind a non-loopback interface without an auth provider, so basic auth has to be there for the healthcheck to pass. Move to OAuth (`hermes dashboard register`) if Hermes is reachable beyond localhost.
- Keep the Hermes LightRAG MCP allowlist as a security boundary for unattended API sessions (five read-oriented tools). The bootstrap drops unfiltered clone duplicates so nothing routes around it.
- Enable SearXNG rate limiting (`--profile searxng-prod`) if the instance is shared on a LAN.

## Dependency updates

Container images are pinned to version tags in `docker-compose.yml` and `.env.example`. Bump deliberately, re-run `make doctor`, and monitor upstream CVEs for production-like deployments.
