# Credentials

Website logins the agent signs in with from its browser. The seam is a credential backend
the agent calls over HTTP: the agent asks to fill a login into a page, and the backend writes
the value into the page itself. Passwords and TOTP codes never reach the model or the agent
process. Service configuration secrets, such as database passwords, are a different category;
see [secrets](secrets.md).

## Interface

A credential backend gives the agent's browser:

| Operation | What it does |
|---|---|
| List logins | Opaque handles with a label, the username, the origins the login is for and whether it has TOTP. No values |
| Fill | Writes the password or a TOTP code for a handle into a field of a browser page, given the page's CDP target. Refuses a page whose origin isn't one of the login's. Returns whether it filled, the origin and redaction digests of the value, never the value |
| Request access | Records that the agent asked for a login it doesn't have, for a site and with a reason. The person approves outside the chat |
| Release | Drops a handle at once, so list and fill refuse it |
| Account state | Each account's mode and state (unlocked, locked, failing), so the agent can say why a fill isn't possible |

The redaction digests let the agent runtime scrub a filled value from anything the model would
later read, such as a page dump or a later browser call, without knowing the value.

Unlocking an account that needs a person's master password happens on a channel the agent
can't reach. Scheduled runs get none of the credential tools.

On the agent side an add-on declares its backend in the manifest's `vaultBackend` block:
`name`, the agent plugin directory, the plugin's `toolset` and `tools`, and `profiles`, which
identities get it. `vault-plan` in [tools](tools.md#plans-for-the-agent-side) prints it for the
agent adapter, and `cron_fence_toolsets()` keeps the toolset out of scheduled runs. The names
`vault`, `op` and `bw` are reserved for the login backends Hermes ships.

## Current implementation

`bitwarden-broker`, built from `compose/bitwarden-broker/` and part of the Bitwarden add-on
(profile `bitwarden`). It reads logins through the `bw` CLI from one or more Bitwarden accounts
(cloud, self-hosted or the local Vaultwarden) and serves only the items in each account's agent
folder or collection. [Bitwarden](../bitwarden.md) covers accounts, modes, unlock and setup.

The broker runs in the `hermes` container's network namespace (`network_mode: "service:hermes"`)
so it can reach Chromium's loopback CDP port, and listens on `127.0.0.1:8099`. Routes, from
`compose/bitwarden-broker/broker/server.py`:

| Route | Operation |
|---|---|
| `GET /items` | list logins; a handle is `bwbroker:<account slug>/<item id>` |
| `POST /fill` | `{handle, target_id, port, field?, base?}`; `field` is `password` (default) or `totp`; `base` is the redaction base the gateway sends |
| `GET /requests`, `POST /requests` | pending and recent access requests; `{site, account?, reason?}` |
| `POST /release` | `{handle}` |
| `GET /accounts` | each account's slug, mode, scope, state, error class and retry time |
| `POST /sync` | sync every account now |
| `GET /health` | status and each account's state |

Bodies are JSON objects of at most 64 KiB with no unknown or duplicate keys, and an error body is
`{"error": "<code>"}` with a fixed code. The access log records method, route, status and handle,
never bodies, item names or sites. `POST /unlock` and `POST /lock`, for accounts in `manual`
unlock mode, are served only on the Unix socket at `BITWARDEN_SYNC_SOCKET`, which is mode 600 and
not reachable from the `hermes` side (`python3 -m broker.unlock`).

The Hermes side is the `bwbroker` plugin (`compose/hermes/plugins/bwbroker`), which registers the
`bwbroker` login backend for `browser_vault` and the `bitwarden_request_access` and
`bitwarden_release_access` tools on the default and `browser` profiles. The overlays
`patch-bitwarden-broker-fill.py` and `patch-digest-redaction.py` route a `bwbroker:` fill to the
broker and scrub filled values. See [agent](agent.md#browser-and-credential-fill).

These parts are specific to the current implementation and not part of the interface:

- Sharing the `hermes` network namespace, which ties the broker's lifecycle to the `hermes`
  container. A browser service of its own, planned, would remove it.
- Account files in `compose/bitwarden/accounts/<slug>.env`. The broker is planned to read the
  add-on's instance store directly instead.
- The `bw` CLI, sync interval (`BITWARDEN_SYNC_MINUTES`) and idle lock
  (`BITWARDEN_IDLE_LOCK_MINUTES`).

## Consumers

| Consumer | Uses |
|---|---|
| Hermes, default and `browser` profiles | the plugin's tools and `browser_vault` fills through the broker |
| Boundary | `scripts/addon.sh connect` and `disconnect bitwarden` to add and remove accounts; see [Bitwarden](../bitwarden.md#boundary) |
