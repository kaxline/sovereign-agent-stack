# Calendar (CalDAV MCP)

Opt-in CalDAV bridge for Hermes. One sidecar speaks CalDAV to any RFC 4791 server, and Hermes registers **one MCP server per account**, so personal and work calendars stay distinct. It is the built-in add-on `caldav` ([addons/caldav/addon.json](../addons/caldav/addon.json)), switched on by `CALDAV_MCP_ENABLED` and the **`calendar`** compose profile.

## Quick path

```bash
./scripts/setup.sh --calendar        # or: scripts/addon.sh enable caldav
make up
read -rs CALDAV_PASSWORD             # iCloud: an app-specific password; not echoed
printf '{"url": "%s", "username": "%s", "password": "%s"}' \
  https://caldav.icloud.com/ you@icloud.com "$CALDAV_PASSWORD" \
  | scripts/addon.sh connect caldav --instance personal
unset CALDAV_PASSWORD
```

In WebUI: *What's on my personal calendar today?*

`addon.sh connect` stores the account and reloads Hermes. It doesn't log in to the CalDAV server first, so a wrong password shows up as failing tool calls; `make doctor` and `hermes mcp list` show whether the entry is there. The `printf` line assumes the values hold no `"` or `\`, which is true of iCloud app-specific passwords.

## How it works

| Piece | Role |
|---|---|
| `caldav-mcp` | Streamable HTTP MCP at `http://caldav-mcp:8080/mcp` (compose-internal only) |
| `data/addons/caldav/` | The add-on's store: `instances/<slug>.json` holds `url` and `username`, `secrets/<slug>.password` the password (mode 600) |
| Hermes bootstrap | Registers `mcp_servers.caldav-<slug>` on the default and `browser` profiles through the manifest's `mcp.instances.headers`. URL and username are literal headers. The password header is `${CALDAV_<SLUG>_PASSWORD}`, and the secret is written to that profile's `.env` |

Upstream: [gelse/caldav-mcp](https://github.com/gelse/caldav-mcp). Credentials are **not** baked into the sidecar; each Hermes MCP entry sends account headers on every call.

## Multi-account

Connect each account under its own slug: a lowercase letter followed by up to 30 of `a-z`, `0-9` and `-`. Hermes names the server `caldav-personal`, `caldav-work`, and so on. Ask for a specific account by name ("check my **work** calendar").

```bash
scripts/addon.sh connect caldav --instance work < work.json
scripts/addon.sh connect caldav --instance work --force < work.json   # replace it
scripts/addon.sh disconnect caldav --instance work
```

Disconnecting removes the account, its `caldav-<slug>` entries and its `CALDAV_<SLUG>_PASSWORD` keys on the next bootstrap, which `disconnect` runs.

## Calendars registered by other clients

An app that runs this stack can register calendars through the Hermes gateway instead of the add-on store: `mcp.servers.add` with the `caldav-mcp` URL and `X-Caldav-*` headers, `mcp.servers.set_api_key` for the password, then `reload.mcp`. No bootstrap run or restart is needed, and only the profiles the app registers on get the entry and the password. Boundary does this, naming entries `calendar-<slug>` with `BOUNDARY_CALENDAR_<SLUG>_PASSWORD` keys.

Use any prefix except `caldav-`. The bootstrap owns `caldav-*` entries: it rewrites them from the store and removes any without an account, together with its `CALDAV_<SLUG>_PASSWORD` key, and with `CALDAV_MCP_ENABLED` off it removes them all. It leaves every other entry and key alone, including entries at the same `caldav-mcp` URL, whether the flag is on or off. `make calendar-test` checks both cases. The sidecar still needs the `calendar` profile running.

Set `trust: untrusted` on these entries. caldav-mcp marks its 8 query tools `readOnlyHint: true` and its 6 changes `false`, so Hermes asks before every change and never before a read. The approval carries the event's redacted arguments ([MCP approvals for untrusted servers](extending.md#mcp-approvals-for-untrusted-servers)).

`profiles.create` copies the launch profile's `.env` into a new profile by default, calendar passwords included. When creating an agent without calendar access, pass `mirror_exclude` with your key pattern, for example `["BOUNDARY_CALENDAR_*", "CALDAV_*_PASSWORD"]` ([Keys a new profile leaves out](extending.md#keys-a-new-profile-leaves-out)).

The bootstrap creates `api-server` by cloning the default profile, once, when it doesn't exist yet. A gateway-registered entry on the default profile at that moment is copied without its password key, since named profiles have their own `.env`. Its calls then fail to authenticate. Remove it from `api-server` with `mcp.servers.remove`.

## Providers

One MCP implementation covers all CalDAV servers. Only URL and auth change.

| Provider | Typical `url` | Auth notes |
|---|---|---|
| **iCloud** | `https://caldav.icloud.com/` | [App-specific password](https://support.apple.com/en-us/102654) required (not your Apple ID password) |
| Nextcloud | `https://host/remote.php/dav/calendars/user/` | Account password or app token |
| Fastmail | `https://caldav.fastmail.com/dav/calendars/user/` | App password recommended |
| Radicale / Baikal | `http(s)://host:5232/user/` (path varies) | Local credentials |
| Google via CalDAV | `https://apidata.googleusercontent.com/caldav/v2/` | Prefer a Google Calendar MCP instead |

Several calendars under **one** login share one account; `caldav_list_calendars` returns all of them.

### iCloud pitfalls

- Wrong password type (Apple ID password instead of app-specific) fails auth.
- 2FA must be on before Apple will issue an app-specific password.

## Who gets the tools

| Hermes profile | Tools |
|---|---|
| default (dashboard / CLI) | All 14 (read + write + attendees) |
| `browser` (WebUI) | All 14 |
| `api-server` (n8n / unattended) | None |
| Cron jobs | None, unless a job names `caldav-<slug>` in its own `enabled_toolsets` |

Calendar is personal data (`permissions.reads: ["calendar"]`), and the add-on manifest rules keep personal data off `api-server` and out of cron toolsets ([addons/README.md](../addons/README.md)). Before 0.3.15 `api-server` got the 8 read tools and cron jobs on the default profile got all 14.

## Security

- The store under `data/addons/caldav/` is gitignored with the rest of `data/`. Passwords are in `secrets/` (mode 600); `addon.sh list --json` shows only whether one is set.
- Bootstrap stores `X-Caldav-Password: ${CALDAV_<SLUG>_PASSWORD}` in Hermes config (`personal` becomes `CALDAV_PERSONAL_PASSWORD`, `work-cal` becomes `CALDAV_WORK_CAL_PASSWORD`). The password itself is written to `data/hermes/.env` and to `browser`'s `.env`. Hermes expands the reference when it connects. This image sends an unset name as the literal placeholder, so the env key has to be there.
- The password still lives under gitignored `data/hermes/`. Treat that tree like a password store. Config dumps and `hermes config` do not include it.
- No host port publish for `caldav-mcp`.
- Do not paste app passwords into tracked docs.

## Verification

```bash
make doctor
docker compose exec caldav-mcp python -c "import socket; s=socket.create_connection(('127.0.0.1',8080),5); s.close(); print('caldav-mcp ok')"
docker compose exec hermes hermes mcp list
```

Expect `caldav-<slug>` on the default and `browser` profiles and none on `api-server`.

## Related

- [Hermes Agent](hermes.md) — MCP registration patterns
- [SECURITY.md](../SECURITY.md) — secrets and API allowlists
