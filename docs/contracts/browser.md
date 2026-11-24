# Browser

The web browser the agent drives: navigating, reading and filling pages. The seam is a Chrome
DevTools Protocol (CDP) endpoint that each client reaches over a WebSocket with its own token.
The agent runtime is one client; the [credential backend](credentials.md), which writes logins
into a page, is another. The Hermes profile named `browser` (the WebUI chat identity) is not part
of this category.

The stack ships a browser service of its own, `web-browser`, off by default
(`WEB_BROWSER_ENABLED=0`). A Hermes profile with no `browser.cdp_url` uses the Chromium bundled in
the Hermes image instead. This page states the interface a browser provider
has to meet, then the browsers the stack can use today.

## Interface

A browser provider gives each client:

| Property | Requirement |
|---|---|
| Endpoint | `ws://<host>:<port>/devtools/browser/<name>?token=<token>`, CDP at browser level. The `/devtools/browser/` path lets Hermes connect without `/json/version` discovery; a client must not need that route |
| Authentication | A token per client, at least 128 bits, compared in constant time. A request with an `Origin` header is refused. The agent and the credential backend never share a token |
| Concurrent clients | Several connections at once: a driver, a dialog supervisor and the credential backend. A CDP session one client creates is private to it; any client may attach to a page target by id |
| Refused methods | Whole-jar cookie reads, writes and clears (`Network.getAllCookies`, `Network.getCookies`, `Network.setCookie(s)`, `Network.deleteCookies`, `Network.clearBrowserCookies`, `Storage.getCookies`, `Storage.setCookies`, `Storage.clearCookies`), the storage clears `Storage.clearDataForOrigin` and `Storage.clearDataForStorageKey`, `DOMStorage.*`, `IndexedDB.*`, `CacheStorage.*`, `Fetch.*`, `IO.read`, `Target.exposeDevToolsProtocol` and `Target.attachToBrowserTarget`. The refusal is a CDP error the agent can report. The `web-browser` gateway's list is in `compose/web-browser/gateway/rules.py` |
| `Fetch` exception | `Fetch.enable` passes when every pattern is a URL under `http://hermes-dialog-bridge.invalid/` and auth interception is off; `Fetch.disable` and the calls that answer a paused request (`Fetch.continueRequest`, `Fetch.continueResponse`, `Fetch.fulfillRequest`, `Fetch.failRequest`) pass. Hermes answers page dialogs through it. Hermes sends `http://hermes-dialog-bridge.invalid/*`, so a provider matches on the host prefix, not the exact string |
| `Browser.close` | Disconnects that client only; the browser keeps running |
| URLs | `file:`, the browser-internal schemes (`chrome:`, `chrome-extension:`, `chrome-untrusted:`, `chrome-search:`, `chrome-devtools:`, `devtools:`) and `view-source:`, which wraps another URL, are refused for navigation and new targets |
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

There are three browsers, chosen per Hermes profile by `browser.cdp_url`.

| Browser | Where | Selected by |
|---|---|---|
| Hermes' Chromium | Launched by browser-harness (`browser_exec`) inside the `hermes` container, from the build shipped in the image under `/opt/hermes/tools`, with a random CDP port on the container's loopback and a fresh profile | no `browser.cdp_url` |
| `web-browser` | A container of its own (`compose/web-browser/`), Debian's Chromium headful on Xvfb behind a CDP gateway that checks one token per client and refuses the methods above. Off by default (`WEB_BROWSER_ENABLED=0`) | `browser.cdp_url` = `ws://web-browser:9222/devtools/browser/stack?token=${WEB_BROWSER_AGENT_TOKEN}`, written by the bootstrap on the attended profiles it manages |
| Boundary's agent browser | Google Chrome on the Mac with its own profile, behind the relay in Boundary's host bridge, which meets the interface above and adds a site allowlist and blocklist the user edits | `browser.cdp_url` set by Boundary to `ws://host.docker.internal:<port>/devtools/browser/boundary?token=…`, with the token in each profile's `.env` as `BOUNDARY_BROWSER_TOKEN` |

Hermes' Chromium has no separate endpoint and no token: only processes in the `hermes` network
namespace reach it, and the Bitwarden broker is no longer one of them, so a login cannot be
filled in it. The broker reaches the other two by provider name: `stack` is the `web-browser`
service's credential client, `boundary` the relay. See
[credentials](credentials.md#current-implementation).

The bootstrap writes the `web-browser` endpoint only on the profiles it manages (the default and
`browser`), never on the unattended `api-server` one, and records what it wrote in
`.web-browser-cdp-url` beside `config.yaml`, so a value Boundary or a person set is left as it is.
The token goes into each of those profiles' `.env`.

The browser tools, the dialog supervisor and the vault actions are described in
[hermes.md](../hermes.md#browser-tools). With the service off, `./scripts/doctor.sh` checks that
the `hermes` user can run the image's Chromium.

## Consumers

| Consumer | Uses |
|---|---|
| Hermes, every profile | `browser_exec`, `browser_cdp`, `browser_dialog` and `browser_vault` through the profile's endpoint or its own Chromium |
| Bitwarden broker | `Target.getTargetInfo`, `Target.attachToTarget`, `Runtime.evaluate`, `Runtime.callFunctionOn` and `Target.detachFromTarget` on the page a fill names, plus `Network.setCookies` and `Page.reload` for a sign-in handover, in the provider the request names |
| Boundary | Sets `browser.cdp_url` on the profiles it manages when its agent browser is on |
