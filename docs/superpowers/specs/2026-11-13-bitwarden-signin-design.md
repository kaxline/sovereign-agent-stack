# Bitwarden broker-side sign-in

Status: design, approved section by section by Keith on 2026-11-12 and 2026-11-13; spike passed
2026-11-13. Targets stack 0.3.16. Builds on `2026-11-08-bitwarden-addon-design.md` (the add-on as
built) and must stay consistent with `docs/private/browser-service-design.md` (see
[Relation to the browser service](#relation-to-the-browser-service)).

## Goal

The agent signs in to a website with a Bitwarden login, and the password and TOTP code never reach
anything agent code can read: not the agent's Chromium, not gateway memory, not a tool result, not
a file in `hermes`. Today the broker writes the value into the agent's own tab, where
`browser_exec` code can read the field and page script can send it anywhere
(`docs/bitwarden.md`, "What the agent can and cannot see").

After sign-in the agent's browser holds the site's session (cookies, `localStorage`,
`sessionStorage`), and agent code can read that. Keith accepted this on 2026-11-12: the guarantee
covers the password and TOTP secret, not the session.

Out of scope: sign-up (CR 0024 `create_login` and `capture_secret` still put a generated password
in the agent's page), IndexedDB sessions, a viewer of the sign-in browser for the person, and
Boundary's agent browser (see [Relation to the browser service](#relation-to-the-browser-service)).

## Design overview

A new container, `bitwarden-signin`, runs a private Chromium in its own network namespace. The
agent drives that Chromium through a short list of actions that never return a value and never
run agent-supplied script. The broker hands the signer the password or TOTP code only at the moment
it is written into a password or one-time-code field. When the agent finishes, the broker copies
the site's session into the agent's tab and the signer kills its Chromium.

```
hermes netns (agent code reaches everything here)
  ├─ hermes: gateway, browser-harness Chromium (the agent's browser)
  └─ bitwarden-broker 127.0.0.1:8099 (no auth, returns no secrets)
         │  Unix socket in the bitwarden_signin volume (broker and signer only)
         ▼
bitwarden-signin (own netns; network `signin` with egress)
  ├─ Chromium, CDP on its own loopback, started per session
  └─ signer server, listening only on the socket
```

Today's in-page fill stays for items the user marks for it (see [Opt-in in-page fill](#opt-in-in-page-fill)).

## Spike results

Ran 2026-11-13 in scratch containers from `nousresearch/hermes-agent:v0.23.1` against a throwaway
local login site with two flows: a form that sets an HttpOnly cookie, and a page whose script
stores a bearer token in `localStorage`. The signer ran with a read-only root, a 512 MB tmpfs at
`/tmp`, all capabilities dropped, `no-new-privileges`, a 768 MB memory limit and a non-root uid.
The agent side ran the same way in a second container with its own network namespace. Nothing
touched the running stack, and the code was not kept.

- **Handover works for both flows.** The agent Chromium showed "login required" before and
  "signed in" after `Network.setCookies` (cookie flow) or a `localStorage` write on the site's
  origin (token flow) followed by a reload. The HttpOnly cookie stayed invisible to page script
  after the handover.
- **The canary password never reached the agent side**: not in the handover data, the agent's DOM,
  its `localStorage` or any file in its profile directory.
- **Cost.** Chromium started in 0.3 to 0.5 s. The container's memory peaked at about 168 MB with a
  login page loaded, 227 MB with Chromium's sandbox on, and fell to 27 to 30 MB (17 MB before
  Chromium) once Chromium was killed and its profile deleted.
- **Chromium's sandbox does not start under Docker's default seccomp profile**, which blocks the
  user namespaces it needs. With `seccomp=unconfined` (diagnostic only) it starts and signs in.
  Hermes' own launcher passes `--no-sandbox` in Docker (`tools/browser_launcher.py`). See
  [Chromium sandbox](#chromium-sandbox).

Not checked by the spike: sites that bind a session to the IP address or to a device key, real
multi-step and SSO flows, and the accessibility snapshot.

## Containers

### `bitwarden-signin`

`compose/bitwarden-signin/`: an image built `FROM` the pinned Hermes image the stack runs, so its
Chromium build and user agent match the agent's browser. The rebuild on each Hermes bump keeps them
matched. It adds only `signer/` (Python, stdlib plus the `websockets` already in the image's venv).
The extra layer is a few MB; the base layers are shared with `hermes`.

Service settings, profile `bitwarden`:

- `user: "${HERMES_UID}:${HERMES_GID}"`, as the broker runs.
- `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, the seccomp profile from
  [Chromium sandbox](#chromium-sandbox).
- tmpfs `/tmp` (512 MB), which holds the per-session Chromium profile.
- `mem_limit: 768m`.
- Network `signin` only: a new bridge network with egress, which no other service joins. Nothing in
  the container listens on a network interface; Chromium's CDP binds the container's loopback.
- Volume `bitwarden_signin` at `/run/bitwarden-signin`, mounted by the broker and the signer only.
  The signer listens on `/run/bitwarden-signin/signin.sock`, mode 0600.
- Environment: the socket path, the idle timeout, the per-action timeout. Nothing else
  (`test-compose-env-allowlist.py` gains the service).
- Healthcheck: a `GET /health` over the socket.

The signer needs no `follow-hermes`: it is not in the `hermes` namespace. Compose creates the
volume and the network, so `pre-up` has nothing new to create.

### Broker changes

The broker stays in the `hermes` namespace in 0.3.16 (see
[Relation to the browser service](#relation-to-the-browser-service)). It mounts `bitwarden_signin`,
forwards the agent's sign-in actions to the signer over the socket, and writes the handover into the
agent's tab over its existing CDP path (`broker/cdp.py`). It remains the only process that decrypts
items.

### Chromium sandbox

The signer loads pages from the open web, so a renderer exploit there is the main risk to what the
signer holds: the value of the current session and the session cookies. The container is the first
boundary (no capabilities, read-only root, its own network, no route to the broker but a socket on
which the signer is the server). Chromium's sandbox would be a second one.

Decision: ship `compose/bitwarden-signin/seccomp.json`, Docker's default profile plus the `clone`,
`clone3` and `unshare` cases that create a user namespace, so Chromium's namespace sandbox runs.
The build checks it on Docker Desktop (macOS) and on a Linux host. Where it can't start, the signer
reports `sandbox_unavailable` in its health and refuses sessions; there is no silent fall back to
`--no-sandbox`. If the profile proves unworkable on either host, the build stops and asks Keith
whether to run with `--no-sandbox`, as Hermes' own browser does.

## The agent's actions

New `browser_vault` actions, routed by the fill overlay for `bwbroker:` handles to new broker
routes under `/signin/`. The routes keep the broker's rules: JSON bodies of at most 64 KiB with no
unknown keys, the Host and `Origin` checks, `application/json` only, fixed error codes, and no
value in any response.

| Action | Broker route | Does | Returns |
|---|---|---|---|
| `sign_in {handle}` | `POST /signin/start` | Starts a session, opens the item's first URI, runs the automatic sequence | `{done: true, origin, cookies}` after a handover, or a snapshot |
| `signin_snapshot` | `POST /signin/snapshot` | Reads the page | snapshot |
| `signin_click {ref}` | `POST /signin/click` | Clicks the element | snapshot |
| `signin_type {ref, text}` | `POST /signin/type` | Types agent-supplied text into a field that isn't a password field | snapshot |
| `signin_fill {ref, field}` | `POST /signin/fill` | Writes the password (`field: "password"`) or current TOTP (`"totp"`) into that field | snapshot |
| `signin_finish` | `POST /signin/finish` | Hands the session to the agent's tab, ends the session | `{done: true, origin, cookies}` |
| `signin_cancel` | `POST /signin/cancel` | Ends the session | `{cancelled: true}` |

`signin_finish` carries the agent tab's CDP target id and port, as `/fill` does today.

**Snapshot.** The page's accessibility tree (`Accessibility.getFullAXTree`) reduced to role, name,
visible text and a ref per actionable node. Refs are numbered per snapshot and map to backend node
ids inside the signer; a ref from an older snapshot whose node is gone gets `stale_ref`. Every
input's value is left out, whatever its type. Before a snapshot leaves the signer, it is scrubbed
for every value written in this session, in the five encodings the digest redaction uses
(raw, base64, hex, percent-encoded, reversed) with and without CR and LF; a match becomes
`«redacted-vault-secret»`. The snapshot also carries the page's origin.

**Origins.** Every action refuses with `origin_mismatch` unless the page's origin is one of the
item's origins or one of its IdP origins. IdP origins come from an optional custom field on the
item, `agent-signin-idp`, a comma-separated list of origins (for example
`https://accounts.example-sso.com`). `signin_fill` and the handover need an item origin; an IdP
origin is not enough.

**`signin_fill` targets.** `input[type=password]` for `password`. For `totp`, an input with
`autocomplete="one-time-code"`, or the first of a run of one-character inputs, as core's
`enter_code` probes. Anything else gets `not_a_secret_field`. `signin_type` refuses
`input[type=password]` with the same code, so the agent can't use it to probe where a value went.

**Automatic sequence** (`sign_in`): find the username field (`autocomplete=username`, then
`type=email`, then a `name` or `id` containing `user`, `email` or `login`), type the item's
username, then fill a visible password field or submit and look for one on the next page, fill it,
submit. If a one-time-code field appears and the item has a TOTP key, fill it and submit. When no
password or one-time-code field is left and the page is on an item origin, the broker does the
handover and `sign_in` returns `done`. At any other point where the sequence can't continue it
returns the snapshot, and the agent continues with the other actions.

When a page needs a person (a CAPTCHA, a link sent by email, a new-device check), the
`browser-login` skill tells the agent to call `signin_cancel` and tell the user, who signs in
themselves.

## Handover

On `signin_finish` (or the end of the automatic sequence):

1. The signer reads cookies with `Storage.getCookies` and keeps those whose domain matches the host
   of one of the item's origins (host-only cookies on that host, or domain cookies on it or a
   parent domain). IdP cookies stay in the signer.
2. It reads `localStorage` and `sessionStorage` of the top-level page if the page's origin is an
   item origin.
3. The broker calls `focus_page` on the agent's tab for that origin (through the overlay, as fills
   do), checks the tab's origin over CDP with today's `/fill` rules, and refuses with
   `handover_refused` if it isn't an item origin. The signer session stays open then, so the agent
   can open the page and call `signin_finish` again; the idle timeout still applies.
4. It writes the cookies with `Network.setCookies` (name, value, domain, path, expires, httpOnly,
   secure, sameSite) and the storage entries with `Runtime.callFunctionOn`, the entries passed as
   arguments, never as source. Then it reloads the tab.
5. The signer kills Chromium and deletes the profile; the session's values are dropped.

The result is `{done: true, origin, cookies: <count>}`. The agent checks its own page. If the site
shows its login page again (a session bound to the IP address or a device key), the skill tells the
agent to report that the site refused the handover and stop, with no retry.

## Opt-in in-page fill

An item whose Bitwarden custom boolean field `agent-in-page-fill` is true keeps today's path:
`fill_login` and `enter_code` write into the agent's own tab, with digest redaction. For any other
`bwbroker:` item both refuse with fixed text telling the agent to call `sign_in`.
`browser_vault list` gains `sign_in_only: true` on those items so the agent picks the right action
first.

This is a behavior change for every existing Bitwarden item: none has the field, so after the
upgrade none is filled in-page until the user adds it. The CHANGELOG says so.

## Redaction

Sign-in returns no redaction entries. A digest entry lets whoever holds it test guesses offline and
gives away a 6-digit TOTP code at once; the snapshot scrub inside the signer replaces it, since the
signer is out of agent code's reach and can hold the values for the session. The digest path and
`patch-digest-redaction.py` stay for opt-in in-page fills only.

## Lifecycle

- One session at a time. `sign_in` while one is open gets `busy`.
- Chromium starts when a session opens, with a fresh profile in the tmpfs, and is killed with its
  profile deleted on finish, on cancel, after 5 minutes without an action
  (`BITWARDEN_SIGNIN_IDLE_MINUTES`, default 5), or when the broker or the signer restarts.
- One action is bounded by 30 seconds.
- The signer server alone uses about 20 MB; Chromium adds roughly 150 to 210 MB while a session is
  open (spike figures, one simple page).
- The broker's `/health` and `vault.sh status --json` gain `signer`: `ready`, `busy`, `down` or
  `sandbox_unavailable`. `make doctor` reports it.

## Failure codes

Fixed codes, never quoting page text, the value or a script:

| Code | When |
|---|---|
| `busy` | a session is already open |
| `no_session` | an action other than `sign_in` with no session open |
| `stale_ref` | the ref's node is gone |
| `origin_mismatch` | the page is not on an item or IdP origin |
| `not_a_secret_field` | `signin_fill` on, or `signin_type` into, the wrong kind of field |
| `no_totp` | `field: "totp"` on an item without a key, or `bw get totp` fails |
| `no_page` | `sign_in` with no tab of the task on the item's first origin |
| `handover_refused` | the agent's tab is not on an item origin |
| `page_error` | a page exception or a CDP error in the signer |
| `timeout` | an action took more than 30 seconds |
| `signer_unavailable` | the signer doesn't answer on the socket |
| `sandbox_unavailable` | Chromium's sandbox can't start |
| `sign_in_only` | `/fill` on an item without `agent-in-page-fill` |
| `account_locked`, `unknown_handle`, `unavailable` | as `/fill` today |

On `signer_unavailable` or `sandbox_unavailable`, an item marked for in-page fill can still be
filled the old way; any other item fails and the agent tells the user.

## Hermes side

- `patch-bitwarden-broker-fill.py` gains the routing for the seven actions on `bwbroker:` handles
  and the `sign_in_only` refusal of `fill_login` and `enter_code`. The new action names go into the
  `browser_vault` schema through the same overlay. Both stay within the fork cap rule's single
  upstream file; if the schema change needs a second file, the build stops and asks.
- The backend adds `sign_in_only` to item metadata from `/items`.
- The cron fence needs nothing new: in a cron turn the backend lists no items, so every action
  gets `unknown_handle`.
- The `browser-login` skill: `sign_in` first; follow snapshots on a multi-step flow; cancel and
  tell the user at a CAPTCHA, an emailed link or a refused handover.
- `docs/bitwarden.md`, "What the agent can and cannot see", is rewritten: the password and TOTP
  never enter `hermes` for a sign-in; session tokens do; IdP cookies stay in the signer; items
  marked for in-page fill keep the old exposure.

## Relation to the browser service

`docs/private/browser-service-design.md` build step 3 moves the broker out of the `hermes`
namespace, adds an API token (`secrets/bitwarden/api-token`) and replaces `port` in `/fill` with a
`browser` provider name; step 6 names the token file for Boundary's relay. Step 3 can't ship before
the `web-browser` service: with no provider, the broker reaches the in-container Chromium only
from inside `hermes`. So in 0.3.16:

- The broker stays in the `hermes` namespace, without a token, and `follow-hermes` stays.
- This design adds only the signer, the `signin` network and the broker-to-signer socket. It defines
  nothing about the broker's own network or tokens; step 3 still owns those.
- The handover is a write through the broker's connection to the agent's browser, the same one
  `/fill` uses. When step 3 lands it replaces that connection (`port` → `browser`) for both.
- The handover writes cookies and storage. The browser contract (`docs/contracts/browser.md`) and
  Boundary's relay refuse whole-jar cookie and `DOMStorage` methods today; the broker writes
  through `Network.setCookies` and `Runtime.callFunctionOn` on a page session. Step 3 must allow
  `Network.setCookies` for the credential client, limited to the item's origins, or the handover
  won't work through a provider. The step 6 / CR item 4.1 follow-up to Boundary carries the same
  request for its relay. Until then sign-in works only in the in-container Chromium, as fills do
  today.

## Boundary

For the 0.3.16 contract, sent to the sessions "Pin 0.3.14 in labs.11" and "Bitwarden integration
for agent login" once the live check passes:

- New service `bitwarden-signin` under the existing `bitwarden` profile, started by the same compose
  calls. No `follow-hermes` for it.
- New named volume `bitwarden_signin` and network `signin`, created by compose. Nothing for
  Boundary to create before `up`.
- `vault.sh status --json` gains `signer`. Additive.
- `fill_login` and `enter_code` refuse Bitwarden items without `agent-in-page-fill`. Behavior
  change, in the CHANGELOG.
- No change to `pre-up`, `up-hook`, `follow-hermes`, `addon.sh connect/disconnect bitwarden`, or
  `vault.sh requests/unlock/sync --json`.

## Testing

Added to `tests.manifest` and `make bitwarden-test`.

- Signer unit tests: the snapshot leaves out input values; the scrub catches each encoding; refs go
  stale; `signin_fill` refuses non-secret fields and off-origin pages; `signin_type` refuses
  password fields; cookie selection leaves IdP cookies out; idle timeout and cancel kill Chromium
  and delete the profile; a second session gets `busy`.
- Broker tests: `/signin/*` keeps the Host, `Origin` and content-type rules; a canary password is
  absent, byte for byte and in each encoding, from every response of every route; the
  `agent-in-page-fill` field gates `/fill`; `signer` appears in `/health` and `status --json`.
- Overlay test against the image: the seven actions route; `fill_login` on a `sign_in_only` item is
  refused; `list` shows `sign_in_only`.
- End to end, `--image`: local Vaultwarden, the broker, the signer and the Hermes image's Chromium,
  with a local login site serving a one-page form, a two-step form, a TOTP step, a token in
  `localStorage` and an IdP redirect. It runs `sign_in`, the snapshot-driven two-step flow and TOTP,
  then searches for the canary in everything agent code can reach: the agent Chromium's DOM,
  storage and profile directory, gateway process memory through `/proc` as the `hermes` user (as
  in the 2026-11-08 spike), and the logs of every container.
- The sandbox check on Docker Desktop and Linux.

## Live check

After 0.3.15 is tagged, before landing:

1. One model-driven `sign_in` turn on the running stack against the local Vaultwarden and a public
   practice login page.
2. One against Keith's real Bitwarden account on a site he picks.

Results are recorded as metadata only (pass or fail, error codes). No transcript is read.

Then the CHANGELOG under 0.3.16, `docs/bitwarden.md`, and the contract to Boundary.
