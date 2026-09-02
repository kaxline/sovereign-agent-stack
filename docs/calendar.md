# Calendar (CalDAV MCP)

Opt-in CalDAV bridge for Hermes. One sidecar speaks CalDAV to any RFC 4791 server; Hermes registers **one MCP server per account** so personal and work calendars stay distinct.

Requires the **`calendar`** compose profile (`./scripts/setup.sh --calendar`).

## Quick path

```bash
./scripts/setup.sh --calendar
# Edit compose/caldav-mcp/accounts/personal.env (iCloud: app-specific password)
make up
docker compose run --rm hermes-api-bootstrap
docker compose run --rm hermes-browser-bootstrap
docker compose restart hermes
```

In WebUI: *What's on my personal calendar today?*

## How it works

| Piece | Role |
|---|---|
| `caldav-mcp` | Streamable HTTP MCP at `http://caldav-mcp:8080/mcp` (compose-internal only) |
| `compose/caldav-mcp/accounts/<slug>.env` | Per-account `CALDAV_URL` / `USERNAME` / `PASSWORD` |
| Hermes bootstrap | Registers `mcp_servers.caldav-<slug>` with `X-Caldav-*` headers |

Upstream: [gelse/caldav-mcp](https://github.com/gelse/caldav-mcp). Credentials are **not** baked into the sidecar; each Hermes MCP entry sends account headers on every call.

## Multi-account

```bash
cp compose/caldav-mcp/account.env.example compose/caldav-mcp/accounts/work.env
# Edit work.env, then re-run both bootstraps and restart hermes
docker compose run --rm hermes-api-bootstrap
docker compose run --rm hermes-browser-bootstrap
docker compose restart hermes
```

Slug = filename without `.env` (lowercase letters, digits, hyphens). Hermes names the server `caldav-personal`, `caldav-work`, and so on. Ask for a specific account by name (“check my **work** calendar”).

Removing an account: delete the `.env` file and re-run bootstrap (stale `caldav-*` keys are dropped).

## Providers

One MCP implementation covers all CalDAV servers. Only URL and auth change.

| Provider | Typical `CALDAV_URL` | Auth notes |
|---|---|---|
| **iCloud** | `https://caldav.icloud.com/` | [App-specific password](https://support.apple.com/en-us/102654) required (not your Apple ID password) |
| Nextcloud | `https://host/remote.php/dav/calendars/user/` | Account password or app token |
| Fastmail | `https://caldav.fastmail.com/dav/calendars/user/` | App password recommended |
| Radicale / Baikal | `http(s)://host:5232/user/` (path varies) | Local credentials |
| Google via CalDAV | `https://apidata.googleusercontent.com/caldav/v2/` | Prefer a Google Calendar MCP instead |

Several calendars under **one** login still share one account file; `caldav_list_calendars` returns all of them.

### iCloud pitfalls

- Wrong password type (Apple ID password instead of app-specific) fails auth.
- 2FA must be on before Apple will issue an app-specific password.
- If bootstrap skips the account, check for placeholder `you@icloud.com` / `xxxx-xxxx-xxxx-xxxx` still in the file.

## Tool allowlists

| Hermes profile | Tools |
|---|---|
| default (dashboard / CLI) | All 14 (read + write + attendees) |
| `browser` (WebUI) | All 14 |
| `api-server` (n8n / unattended) | Read-only 8: list/get/search/freebusy/list attendees |

Create/update/delete stay off the unattended API profile on purpose (same idea as the LightRAG read-only allowlist).

## Security

- Account files under `compose/caldav-mcp/accounts/*.env` are gitignored (`*.env`).
- Bootstrap copies credentials into Hermes `data/hermes/**/config.yaml` as MCP headers (also under gitignored `data/`). Treat that tree like a password store.
- No host port publish for `caldav-mcp`.
- Do not commit account files or paste app passwords into tracked docs.

## Verification

```bash
make doctor
docker compose exec caldav-mcp python -c "import socket; s=socket.create_connection(('127.0.0.1',8080),5); s.close(); print('caldav-mcp ok')"
docker compose exec hermes hermes mcp list
docker compose exec hermes hermes -p api-server mcp list
```

Expect `caldav-<slug>` on each profile; api-server should show the shorter tool list.

After changing accounts or enabling the profile, re-run bootstrap (it is a one-shot and does not run on every `up`):

```bash
docker compose run --rm hermes-api-bootstrap
docker compose run --rm hermes-browser-bootstrap
docker compose restart hermes
```

## Related

- [Hermes Agent](hermes.md) — MCP registration patterns
- [SECURITY.md](../SECURITY.md) — secrets and API allowlists
