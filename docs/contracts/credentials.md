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
| Sign in | Signs a handle in to its site in a browser the agent cannot reach, step by step, and hands the resulting session to a page of the agent's browser. Returns page snapshots without input values, and the handover's origin and cookie count. No value and no redaction digest |
| Request access | Records that the agent asked for a login it doesn't have, for a site and with a reason. The person approves outside the chat |
| Release | Drops a handle at once, so list and fill refuse it |
| Account state | Each account's mode and state (unlocked, locked, failing), so the agent can say why a fill isn't possible |

The redaction digests let the agent runtime scrub a filled value from anything the model would
later read, such as a page dump or a later browser call, without knowing the value.

Fill and sign-in differ in what the agent ends up holding. A fill puts the value in a page the
agent drives, so the digests are what keep it out of the model's reading; a sign-in keeps the value
on the backend's side, and the agent gets the site's session instead.

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
| `POST /signin/start` | `{handle, target_id, port}`; opens the item's first URI in the signer and runs the automatic sequence. Returns the handover or a snapshot |
| `POST /signin/snapshot` | `{}`; the page's accessibility tree as role, name, text and a ref per actionable node, with the origin and no input values |
| `POST /signin/click` | `{ref}` |
| `POST /signin/type` | `{ref, text}`; agent text, refused on a password field |
| `POST /signin/fill` | `{ref, field}`; `field` is `password` or `totp`, written into a password or one-time-code field only |
| `POST /signin/finish` | `{target_id, port}`; hands the session to that tab and ends the session |
| `POST /signin/cancel` | `{}` |
| `POST /release` | `{handle}` |
| `GET /accounts` | each account's slug, mode, scope, state, error class and retry time |
| `POST /sync` | sync every account now |
| `GET /health` | status, the signer's state (`ready`, `busy`, `sandbox_unavailable`, `down`), each account's state, and each refused account's file and cause |

The `/signin` routes drive a second container, `bitwarden-signin`, over a Unix socket in the
`bitwarden_signin` volume (mode 0600, mounted by the broker and the signer only). One session runs
at a time (`busy`), one action is bounded, and the session's Chromium and profile are destroyed on
finish, on cancel, on the idle timeout and on a restart. The signer scrubs every value the session
wrote out of the snapshots it returns. The handover writes the item's cookies with
`Network.setCookies` and the page's storage entries as call arguments, after checking the target
tab's origin (`handover_refused` otherwise). Codes: `busy`, `no_session`, `stale_ref`,
`origin_mismatch`, `not_a_secret_field`, `no_totp`, `handover_refused`, `page_error`, `timeout`,
`signer_unavailable`, `sandbox_unavailable` and `sign_in_only`, which `/fill` returns for an item
with no `agent-in-page-fill` custom field.

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
  container, and the `port` field in `POST /fill` and `/signin/start`, which only works for Hermes'
  own Chromium. A browser service of its own, planned, would remove both; see [browser](browser.md).
  The handover also needs `Network.setCookies` on the credential client's connection, which the
  browser contract refuses today, so sign-in works only in the in-container Chromium.
- The two custom fields the sign-in reads from an item, `agent-in-page-fill` and
  `agent-signin-idp`, and `sign_in_only` in the item metadata.
- Where an account is defined: the add-on instance store (`instances/<slug>.json`, read directly
  by the broker), with each `client_id` in `secrets/<slug>/client_id`, and the local account in
  `broker/local-account.json`.
- The `bw` CLI, sync interval (`BITWARDEN_SYNC_MINUTES`) and idle lock
  (`BITWARDEN_IDLE_LOCK_MINUTES`).

## Consumers

| Consumer | Uses |
|---|---|
| Hermes, default and `browser` profiles | the plugin's tools and `browser_vault` fills through the broker |
| Boundary | `scripts/addon.sh connect` and `disconnect bitwarden` to add and remove accounts; see [Bitwarden](../bitwarden.md#boundary) |
