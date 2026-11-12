# Browser

The web browser the agent drives: navigating, reading and filling pages. The seam is a Chrome
DevTools Protocol (CDP) endpoint that each client reaches over a WebSocket with its own token.
The agent runtime is one client; the [credential backend](credentials.md), which writes logins
into a page, is another. The Hermes profile named `browser` (the WebUI chat identity) is not part
of this category.

The stack does not yet run the browser as a service of its own. This page states the interface a
browser provider has to meet, then where the browser runs today.

## Interface

A browser provider gives each client:

| Property | Requirement |
|---|---|
| Endpoint | `ws://<host>:<port>/devtools/browser/<name>?token=<token>`, CDP at browser level. The `/devtools/browser/` path lets Hermes connect without `/json/version` discovery; a client must not need that route |
| Authentication | A token per client, at least 128 bits, compared in constant time. A request with an `Origin` header is refused. The agent and the credential backend never share a token |
| Concurrent clients | Several connections at once: a driver, a dialog supervisor and the credential backend. A CDP session one client creates is private to it; any client may attach to a page target by id |
| Refused methods | Whole-jar cookie reads and writes (`Network.getAllCookies`, `Network.getCookies`, `Network.setCookie(s)`, `Storage.getCookies`, `Storage.setCookies`), `DOMStorage.*`, `IndexedDB.*`, `CacheStorage.*`, `Fetch.*`, `IO.read`, `Target.exposeDevToolsProtocol` and `Target.attachToBrowserTarget`. The refusal is a CDP error the agent can report |
| `Fetch` exception | `Fetch.enable` and the calls that answer paused requests pass when every pattern is `http://hermes-dialog-bridge.invalid/` and auth interception is off; Hermes answers page dialogs through it |
| `Browser.close` | Disconnects that client only; the browser keeps running |
| URLs | `file:` and browser-internal schemes (`chrome:`, `chrome-extension:`, `devtools:`) are refused for navigation and new targets |
| Lifetime | The browser outlives agent restarts and sessions. A provider that isn't running refuses the connection rather than holding it open |

Downloads and file uploads happen in the provider's own filesystem. A provider that supports them
says where.

### Agent side

The agent runtime takes one endpoint URL per identity and opens it for every browser tool. In
Hermes that is `browser.cdp_url` in the profile's `config.yaml`, which Hermes reads on each
browser call, so a change needs no restart. With no URL set, Hermes launches its own Chromium
(below).

When the agent asks the credential backend to fill a field, it passes the page's CDP target id
and which provider the page is in. The backend connects to that provider with its own token,
attaches to the target and checks the page's origin before writing. See
[credentials](credentials.md#interface).

## Current implementation

There are two browsers, chosen per Hermes profile by `browser.cdp_url`.

| Browser | Where | Selected by |
|---|---|---|
| Hermes' Chromium | Launched by browser-harness (`browser_exec`) inside the `hermes` container, from the build shipped in the image under `/opt/hermes/tools`, with a random CDP port on the container's loopback and a fresh profile | no `browser.cdp_url` |
| Boundary's agent browser | Google Chrome on the Mac with its own profile, behind the relay in Boundary's host bridge, which meets the interface above and adds a site allowlist and blocklist the user edits | `browser.cdp_url` set by Boundary to `ws://host.docker.internal:<port>/devtools/browser/boundary?token=…`, with the token in each profile's `.env` as `BOUNDARY_BROWSER_TOKEN` |

Hermes' Chromium has no separate endpoint and no token: only processes in the `hermes` network
namespace reach it. The Bitwarden broker shares that namespace for this reason
(`network_mode: "service:hermes"`), and `POST /fill` takes the CDP `port` instead of a provider.
The broker connects only to `127.0.0.1`, so it fills in Hermes' Chromium and refuses a page in
Boundary's browser. See [credentials](credentials.md#current-implementation).

The browser tools, the dialog supervisor and the vault actions are described in
[hermes.md](../hermes.md#browser-tools). `./scripts/doctor.sh` checks that the `hermes` user can
run the image's Chromium.

A stack browser service, a container that serves the interface with its own tokens and network
so the broker no longer shares the `hermes` namespace, is planned and not built.

## Consumers

| Consumer | Uses |
|---|---|
| Hermes, every profile | `browser_exec`, `browser_cdp`, `browser_dialog` and `browser_vault` through the profile's endpoint or its own Chromium |
| Bitwarden broker | `Target.attachToTarget`, `Runtime.evaluate`, `Runtime.callFunctionOn` and `Target.detachFromTarget` on the page a fill names, in Hermes' Chromium only |
| Boundary | Sets `browser.cdp_url` on the profiles it manages when its agent browser is on |
