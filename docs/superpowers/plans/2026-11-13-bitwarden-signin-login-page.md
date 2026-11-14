# Finding the Login Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `sign_in` reaches a login form on a site whose saved Bitwarden URI opens something else — an invitation, a reset link, a deep link.

**Architecture:** The broker builds an ordered list of candidate URLs from the item and keeps the first page whose snapshot holds a login form. The signer gains one route, `/session/navigate`, so the broker can move an open session to the next candidate; `Session.open` already navigates the existing page and already gates on `url_allowed`, so the route adds no reach. URL classification lives in `origins.py` as pure functions.

**Tech Stack:** Python 3.11 (broker image) and 3.13 (Hermes image), stdlib only. Tests are the project's own scripts under `scripts/`, run directly.

**Spec:** `docs/superpowers/specs/2026-11-13-bitwarden-signin-design.md`, section "Finding the login page".

## Global Constraints

- Ships in 0.3.16 on branch `feat/bitwarden-signin`, before landing.
- No new agent-visible failure code. The error table in the spec's "Failure codes" stays as it is.
- At most six navigations in one `sign_in`.
- Every candidate passes the signer's existing `url_allowed`; nothing outside the item's origins plus `idp_origins` is opened.
- Single-use URIs are never navigated to.
- No page values cross the broker: replies stay snapshot (origin, and role, name, text, ref per node) or a fixed code.
- `MAX_URL = 8192`; the signer refuses a longer URL.
- Both files are patched into an upstream image, so keep the fork cap in mind: no new upstream file.

## Review Focus

- A candidate that answers `page_error` (a 404 root, a dead host) must not end the ladder — the next candidate is still tried. Task 2.
- A candidate that redirects off the item's origins gets `origin_mismatch` from the signer; the ladder treats it as a miss and carries on. Task 2.
- `timeout` ends the session on the signer, so the broker must stop the ladder rather than navigate a dead session. Task 2.
- A page with a username field but no password field is a two-step login and stops the ladder. Task 2.
- An item with more URIs than the cap must stop at six navigations. Task 2.

---

### Task 1: `/session/navigate` on the signer

**Files:**
- Modify: `compose/bitwarden-signin/signer/session.py` (add `navigate`, next to `open` at line 142)
- Modify: `compose/bitwarden-signin/signer/server.py:65` (`ROUTES`), and its module docstring at lines 10-14
- Test: `scripts/test-bitwarden-signer.py`

**Interfaces:**
- Produces: `POST /session/navigate` with body `{"url": str, "allowed": list[str]}`, returning the same snapshot dict as `/session/open`. `no_session` (409) without an open session, `origin_mismatch` (403) for a URL outside `allowed`, `page_error` (502) when the navigation fails, `timeout` (504) past `BITWARDEN_SIGNIN_ACTION_SECONDS`, which ends the session.
- Produces: `Session.navigate(url: str, allowed: list[str]) -> dict`.

- [ ] **Step 1: Write the failing tests**

In the image part of `scripts/test-bitwarden-signer.py`, beside the existing session tests:

```python
def test_navigate_moves_an_open_session() -> None:
    # open /login on the test site, then navigate to /second on the same origin
    # expect: the reply is a snapshot whose nodes come from /second
    # expect: no second Chromium started (the session's pid is unchanged)

def test_navigate_without_a_session_is_no_session() -> None:
    # expect: 409 no_session, and no browser started

def test_navigate_refuses_an_origin_outside_allowed() -> None:
    # open on 127.0.0.1, then navigate to the localhost origin with allowed=[127.0.0.1 only]
    # expect: 403 origin_mismatch, and the session survives (a snapshot still works)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 scripts/test-bitwarden-signer.py`
Expected: FAIL — the route is unknown, so the first reply is the no-route canary rather than a snapshot.

- [ ] **Step 3: Add `Session.navigate` and the route**

`Session.navigate(url, allowed)` delegates to `self.open(url, allowed)` — `open` holds no start-up state, so this is one line plus its docstring. Add `"/session/navigate": ("POST", {"url": _URL, "allowed": _ORIGINS})` to `ROUTES`; the generic dispatch at `server.py:227-230` already requires a session and resolves the method by name, so `_open`'s `busy` guard is untouched. Update the module docstring to name the route.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 scripts/test-bitwarden-signer.py`
Expected: PASS

- [ ] **Step 5: Verify the route list is still pinned**

Run: `python3 scripts/test-bitwarden-broker.py`
Expected: PASS — `test_signin_routes_keep_tcp_checks` and `test_no_route_returns_canary` cover the route table; update them if they enumerate routes.

- [ ] **Step 6: Commit**

```bash
git add compose/bitwarden-signin/signer/session.py compose/bitwarden-signin/signer/server.py scripts/test-bitwarden-signer.py
git commit -m "Bitwarden sign-in: a session-scoped navigate route on the signer"
```

---

### Task 2: the candidate ladder in the broker

**Files:**
- Modify: `compose/bitwarden-broker/broker/origins.py` (add `single_use_uri`, `login_candidates`)
- Modify: `compose/bitwarden-broker/broker/signin.py:275-286` (`_open`), and add `_has_login_form` beside `_needs_secret` at line 267
- Modify: `compose/bitwarden-broker/broker/vault.py:33-42` (`ItemMeta` gains `uris`, the fill URIs in order, so the ladder sees more than the first)
- Modify: `docs/bitwarden.md`, `compose/hermes/skills/browser-login/SKILL.md`, `CHANGELOG.md`
- Test: `scripts/test-bitwarden-broker.py`

**Interfaces:**
- Consumes: `POST /session/navigate` from Task 1.
- Produces: `origins.single_use_uri(uri: str) -> bool` and `origins.login_candidates(uris: tuple[str, ...], origin: str, limit: int = 6) -> list[str]`.
- Produces: `ItemMeta.uris: tuple[str, ...]`, the item's fill URIs whole, in stored order (`first_uri` stays, as `uris[0]` when there is one).

- [ ] **Step 1: Write the failing tests for the pure URL functions**

```python
def test_single_use_uri() -> None:
    for uri in ("https://x.test/invite/abc", "https://x.test/accept", "https://x.test/activate/1",
                "https://x.test/password/reset", "https://x.test/verify/9", "https://x.test/confirm",
                "https://x.test/magic", "https://x.test/token/zz", "https://x.test/login?next=/a",
                "https://x.test/login#t", "https://x.test/s/AAAAAAAAAAAAAAAAAAAAAA"):
        expect(single_use_uri(uri), f"single-use: {uri}")
    for uri in ("https://x.test/login", "https://x.test/", "https://x.test/users/sign_in",
                "https://x.test/auth/login"):
        expect(not single_use_uri(uri), f"reusable: {uri}")

def test_login_candidates_order_and_cap() -> None:
    # uris = (invite link, https://x.test/signin), origin https://x.test
    # expect: ["https://x.test/signin", "https://x.test/", then the well-known paths]
    # expect: the invite link appears nowhere
    # expect: len() <= 6, and no duplicates (a stored /login is not repeated by the well-known list)
```

The well-known paths, in this order: `/login`, `/signin`, `/sign-in`, `/auth/login`, `/users/sign_in`, `/account/login`.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 scripts/test-bitwarden-broker.py`
Expected: FAIL with ImportError on `single_use_uri`.

- [ ] **Step 3: Implement `single_use_uri(uri: str) -> bool` and `login_candidates(uris, origin, limit=6) -> list[str]` in `origins.py`**

`single_use_uri`: true when `urlsplit` finds a query or fragment, when any path segment casefolds to a match of `invite|accept|activate|reset|verify|confirm|magic|token`, or when a segment is 20 characters or more and holds no `.` (an opaque id, not a filename). `login_candidates`: the reusable URIs in order, then `origin` itself with a trailing slash, then `origin` joined with each well-known path; drop duplicates by normalized string, drop anything over `MAX_URL`, truncate to `limit`.

- [ ] **Step 4: Run them to verify they pass**

Run: `python3 scripts/test-bitwarden-broker.py`
Expected: PASS for the two new tests.

- [ ] **Step 5: Write the failing ladder tests**

With `FakeSigner` (line 1422), whose `pages` script what the session shows and whose `calls` record `(route, body)`:

```python
def test_open_walks_to_the_first_page_with_a_login_form() -> None:
    # item uris: (invite link, ) only; pages: [UNKNOWN_PAGE (the root), LOGIN_PAGE]
    # expect: calls are /session/open with the origin root, then /session/navigate with
    #         the first well-known path, and the sequence proceeds from the login page
    # expect: the invite URI is in no call body

def test_open_stops_at_a_username_only_page() -> None:
    # pages: [a page with a username field and no password field]
    # expect: exactly one navigation, and the two-step sequence takes it from there

def test_open_carries_on_past_page_error_and_origin_mismatch() -> None:
    # refuse navigate with page_error once, then origin_mismatch once, then a login page
    # expect: the ladder reaches the login page; no SignInError leaves start()

def test_open_stops_the_ladder_on_timeout() -> None:
    # refuse navigate with timeout
    # expect: SignInError("timeout"), no further /session/navigate call, session abandoned

def test_open_caps_navigations() -> None:
    # an item with eight reusable URIs, every page unknown
    # expect: six calls in all (one open, five navigate), then the last snapshot is returned

def test_open_returns_the_last_snapshot_when_nothing_matches() -> None:
    # every candidate unknown
    # expect: start() returns a snapshot, not an error, and the handle stays open for
    #         signin_click (status() is busy)
```

- [ ] **Step 6: Run them to verify they fail**

Run: `python3 scripts/test-bitwarden-broker.py`
Expected: FAIL — `_open` makes one `/session/open` call with the item's first URI.

- [ ] **Step 7: Expose the item's whole URI list**

`vault.py` already computes `_fill_uris`. Add `uris: tuple[str, ...]` to `ItemMeta` beside `first_uri` and set it in `_account_items` (lines 148-151) from the same `_fill_uris` pass. `first_uri` keeps its meaning for the handover comment at `signin.py:277`.

- [ ] **Step 8: Rewrite `SignIn._open(item)` as the ladder, and add `_has_login_form`**

`_has_login_form(snapshot) -> bool` is `_find(nodes, "password") is not None or _find(nodes, "username") is not None`. `_open` builds `origins.login_candidates(item.uris, item.origins[0])`, calls `/session/open` for the first and `/session/navigate` for each one after, and returns the first snapshot `_has_login_form` accepts, else the last snapshot it got. `page_error` and `origin_mismatch` from a candidate are a miss; every other `SignInError` propagates, keeping the existing `_end_session()` on a failed open. Keep the existing comment's intent: the stored URI still goes first when it is reusable.

- [ ] **Step 9: Run them to verify they pass**

Run: `python3 scripts/test-bitwarden-broker.py`
Expected: PASS

- [ ] **Step 10: Update the test that pinned the old behavior**

`test_sequence_opens_the_item_uri` (line 1627) asserts the old one-navigation rule. Re-point it at a reusable URI, so it still proves the stored URI is preferred, and rename it `test_sequence_opens_a_reusable_item_uri_first`.

Run: `python3 scripts/test-bitwarden-broker.py`
Expected: PASS, with the whole file green.

- [ ] **Step 11: Documentation**

`docs/bitwarden.md`: in the sign-in section, say that `sign_in` opens the item's reusable URIs, then the site root, then a short list of well-known login paths, keeps the first page with a login form, and never opens a single-use link. `SKILL.md`: one line in the snapshot guidance at line 107 — when the page is not the login form but the snapshot holds a link or button back to it, `signin_click` that ref instead of `signin_cancel`. `CHANGELOG.md`, under `## Unreleased`: one bullet for the ladder, one for the signer route.

- [ ] **Step 12: Run the Bitwarden suite**

Run: `python3 scripts/test-bitwarden-broker.py` then `python3 scripts/test-bitwarden-signer.py` then `python3 scripts/test-bitwarden-fill.py`
Expected: PASS. `make bitwarden-test` is the full run and belongs to the live check, not to this step.

- [ ] **Step 13: Commit**

```bash
git add compose/bitwarden-broker/broker/origins.py compose/bitwarden-broker/broker/signin.py compose/bitwarden-broker/broker/vault.py scripts/test-bitwarden-broker.py docs/bitwarden.md compose/hermes/skills/browser-login/SKILL.md CHANGELOG.md
git commit -m "Bitwarden sign-in: find the login page instead of trusting the item's URI"
```
