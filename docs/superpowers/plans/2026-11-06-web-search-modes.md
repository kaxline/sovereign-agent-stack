# Web Search Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One `.env` setting, `HERMES_WEB_SEARCH_MODE`, chooses whether Hermes' searxng MCP tools are served by SearXNG, by the keyless ring, or by one with the other as fallback.

**Architecture:** A stdlib module `compose/hermes/web_search_mode.py` holds the mode logic and wraps an MCP tool handler. A cont-init overlay `patch-web-search-mode.py` copies it into `/opt/hermes/agent/` and makes `_make_tool_handler` in `tools/mcp_tool_handlers.py` return the wrapped handler for the searxng server's two tools. The ring side calls upstream's `plugins.web.keyless_mcp` in-process.

**Tech Stack:** Python 3 stdlib (host tests), Hermes v0.22.7 image (image driver), POSIX sh / bash (cont-init, doctor).

**Spec:** `docs/superpowers/specs/2026-11-06-web-search-modes-design.md`

## Global Constraints

- Values: `private` (default, also unset or empty), `private-first`, `keyless-first`, `keyless`. Anything else behaves as `private` and logs one warning per process naming the value.
- `private` mode returns upstream's handler result unchanged, byte for byte.
- Only server key `searxng` with tools `searxng_web_search` and `web_url_read` are wrapped.
- The native `web` toolset stays disabled; no bootstrap change.
- Ring answers are never cached by the overlay.
- Source line on ring-served text: `Served by the keyless web tier (<vendor>).` Extract does not report its vendor, so page reads use `Served by the keyless web tier.`
- Fallback-used line: `<side> was unavailable for this <search|page read> (<first 200 chars of its error>).` where `<side>` is `SearXNG` or `The keyless web tier`.
- Both sides failed: first side's failure with ` (fallback via <SearXNG|the keyless web tier> also failed: <its error>)` appended; a tool error stays a tool error.
- Ring disabled error text: `keyless web tier disabled (web.keyless_fallback: false)`.
- Default ring search limit: 10. `num_results` overrides.
- Overlay conventions: marker comment, exact anchors, `SystemExit` naming the missing anchor, idempotent second run printing `already applied`.
- Tests are stdlib, no pytest, no network. Writing style rules apply to docs, comments and commit messages. No "claude" in branch names, no co-author trailers.

## Review Focus

1. **The gateway process may not see `HERMES_WEB_SEARCH_MODE`.** Under s6, cont-init scripts get no environment; the gateway is started by `main-wrapper.sh`. Expect the setting to work after `docker compose up -d hermes`. Task 5 checks it in the running gateway, not only in `docker exec`.
2. **A profile `.env` that sets the same variable.** Hermes loads profile env files into `os.environ`. Expect the container value to win, or the docs to say which wins. Task 5 checks this with a profile env entry.
3. **`web_url_read` called with a non-http URL or no URL in a ring mode.** Expect no ring call; the call goes to the plain fetch, which gives its own error. Test in Task 2.
4. **A ring search returning `success: true` with zero results.** Expect it treated as a clean empty answer, not a failure, so `keyless-first` does not then send the query to SearXNG. Test in Task 2.
5. **Upstream handler raising instead of returning an error string.** Expect the wrapper to treat the exception as a failure in fallback modes and re-raise in `private` mode. Test in Task 2.

---

## File Structure

- Create `compose/hermes/web_search_mode.py`: mode parsing, failure checks, rendering, the handler wrapper. Imports upstream only inside functions, so the host test can import it.
- Create `compose/hermes/patch-web-search-mode.py`: installs the module and patches `_make_tool_handler`.
- Create `scripts/test-web-search-mode.py`: host tests plus an image driver via `scripts/lib/hermes_image.py`.
- Modify `compose/hermes/05-patch-tool-calling.sh`: run the patch after `patch-blocked-fetch-hint.py`.
- Modify `docker-compose.yml`: mount the two files at `/bootstrap/...:ro` next to `blocked_fetch_hint.py` (around line 1655); add `- HERMES_WEB_SEARCH_MODE=${HERMES_WEB_SEARCH_MODE:-private}` next to `- SEARXNG_URL=` (around line 1518).
- Modify `scripts/doctor.sh`, `docs/searxng.md`, `docs/hermes.md`, `.env.example`, `CHANGELOG.md`.

---

### Task 1: Mode parsing, failure checks and rendering

**Files:**
- Create: `compose/hermes/web_search_mode.py`
- Test: `scripts/test-web-search-mode.py`

**Interfaces:**
- Produces:
  - `MODES: tuple[str, ...] = ("private", "private-first", "keyless-first", "keyless")`
  - `current_mode(environ: Mapping[str, str] = os.environ) -> str`
  - `search_failed(result: Any) -> bool`: the handler JSON string is a tool error, or has no results and names unavailable/unresponsive engines.
  - `fetch_failed(args: Any, result: Any) -> bool`: tool error, or `hint_for("mcp__searxng__web_url_read", args, result)` is not None.
  - `render_search(resp: dict, args: dict) -> str`: handler JSON (`{"result": text}`) for a successful ring response.
  - `render_extract(entry: dict, args: dict) -> str`: handler JSON for a successful ring extract entry.
  - `is_tool_error(result: Any) -> bool`: parsed JSON object with an `error` key.

`hint_for` is imported as `from agent.blocked_fetch_hint import hint_for`, falling back to `from blocked_fetch_hint import hint_for` (host).

- [ ] **Step 1: Write the failing tests**

In `scripts/test-web-search-mode.py`, follow `scripts/test-blocked-fetch-hint.py`'s layout (`fail`, `expect`, `err`, `res`, `main`). Tests:

```python
def test_current_mode():
    expect(wsm.current_mode({}) == "private", "unset")
    expect(wsm.current_mode({"HERMES_WEB_SEARCH_MODE": ""}) == "private", "empty")
    for m in wsm.MODES:
        expect(wsm.current_mode({"HERMES_WEB_SEARCH_MODE": f" {m.upper()} "}) == m, m)
    expect(wsm.current_mode({"HERMES_WEB_SEARCH_MODE": "keyless-only"}) == "private", "bad value")

def test_search_failed():
    expect(wsm.search_failed(err("MCP server 'searxng' is unreachable after 3 consecutive failures.")), "tool error")
    expect(wsm.search_failed(res("No results found for \"x\".\n\n⚠️ Some search engines were unavailable: bing (CAPTCHA). Results may be incomplete.")), "empty + engines")
    expect(wsm.search_failed(res("No results found for \"x\".\n\n⚠️ Some search engines were unavailable: search-gate (busy: the search is queued; ask again in a minute or two). Results may be incomplete.")), "gate busy")
    expect(wsm.search_failed(res(json.dumps({"results": [], "unresponsive_engines": [["bing", "CAPTCHA"]]}))), "json empty + engines")
    expect(not wsm.search_failed(res("No results found for \"x\".")), "clean empty")
    expect(not wsm.search_failed(res("Title: A\nDescription: b\nURL: https://a.example.com\n\n⚠️ Some search engines were unavailable: bing (CAPTCHA). Results may be incomplete.")), "partial results")

def test_fetch_failed():
    a = {"url": "https://registry.example.com/x"}
    expect(wsm.fetch_failed(a, err("\U0001F6AB Website Error (403): Access blocked (bot detection or geo-restriction)")), "403")
    expect(wsm.fetch_failed(a, err("\U0001F310 Network Error: fetch failed")), "tool error")
    expect(not wsm.fetch_failed(a, res("# Acme\n\n" + "text " * 50)), "good page")

def test_render():
    resp = {"success": True, "data": {"web": [{"title": "A", "url": "https://a.example.com", "description": "about a"}], "served_by": "parallel"}}
    text = json.loads(wsm.render_search(resp, {"query": "q", "time_range": "week"}))["result"]
    expect(text.startswith("Served by the keyless web tier (parallel)."), text)
    expect("Title: A\nDescription: about a\nURL: https://a.example.com" in text, text)
    expect("time_range" in text and "not applied" in text, "ignored filter note")
    j = json.loads(json.loads(wsm.render_search(resp, {"query": "q", "response_format": "json"}))["result"])
    expect(j == {"results": [{"title": "A", "url": "https://a.example.com", "content": "about a"}], "served_by": "parallel"}, j)
    page = {"url": "https://a.example.com", "title": "A", "content": "0123456789"}
    text = json.loads(wsm.render_extract(page, {"url": page["url"], "startChar": 2, "maxLength": 3}))["result"]
    expect(text.startswith("Served by the keyless web tier.") and text.endswith("234"), text)
    text = json.loads(wsm.render_extract(page, {"url": page["url"], "section": "Intro"}))["result"]
    expect("whole page" in text and "0123456789" in text, text)
```

When `served_by` is absent, the vendor is the `name` passed to the ring; `render_search` takes it from `resp["data"].get("served_by")`, and Task 2 sets it before rendering.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 scripts/test-web-search-mode.py`
Expected: `ModuleNotFoundError: No module named 'web_search_mode'`

- [ ] **Step 3: Implement the five functions in `compose/hermes/web_search_mode.py`**

`current_mode` strips and lowercases; an invalid value logs `logger.warning("HERMES_WEB_SEARCH_MODE=%r is not one of %s; using private", ...)` once per process (module-level set). Empty-with-engines detection for text: no line starting `URL: ` and the text contains `Some search engines were unavailable`; for JSON: `results == []` and a non-empty `unresponsive_engines`. Ignored-filter note names each of `pageno`, `time_range`, `language`, `safesearch` that was set: `The keyless web tier does not support <names>; they were not applied.` Section/paragraph/headings note: `The keyless web tier returned the whole page; section, paragraphRange and readHeadings were not applied.`

- [ ] **Step 4: Run to verify it passes**

Run: `python3 scripts/test-web-search-mode.py`
Expected: `ok` (the image part is skipped until Task 3 adds a driver)

- [ ] **Step 5: Commit**

```bash
git add compose/hermes/web_search_mode.py scripts/test-web-search-mode.py
git commit -m "Add mode parsing and result checks for Hermes web search modes"
```

### Task 2: Handler wrapper with ring calls and guards

**Files:**
- Modify: `compose/hermes/web_search_mode.py`
- Test: `scripts/test-web-search-mode.py`

**Interfaces:**
- Consumes: Task 1's functions.
- Produces:
  - `wrap_handler(server_name: str, tool_name: str, inner: Callable[..., Any]) -> Callable[..., Any]`: returns `inner` itself unless `server_name == "searxng"` and `tool_name in {"searxng_web_search", "web_url_read"}`.
  - Seams the tests replace: `_ring_search(query: str, limit: int) -> dict`, `_ring_extract(url: str) -> dict`, `_ring_enabled() -> bool`, `_url_refusal(url: str) -> Optional[str]` (handler-JSON refusal or None).

`_ring_search` calls `plugins.web.keyless_mcp.search_with_failover("exa", query, limit)` and sets `data["served_by"]` to `"exa"` when absent. `_ring_extract` calls `extract_with_failover("exa", [url])[0]`. `_ring_enabled` calls `keyless_enabled()`. `_url_refusal` uses `tools.web_tools_extract._validate_extract_urls([url])` (blocked JSON → return it) and then `tools.url_safety.is_safe_url(url)` (sync; false → `tool_error("Blocked: URL targets a private or internal network address")`). The handler is sync, so use `is_safe_url`, not `async_is_safe_url`.

The wrapped handler reads `current_mode()` on every call, so the mode follows the environment without a restart of the module. Order per mode follows the spec table. A ring failure becomes an error string: search uses `resp["error"]`; extract uses the entry's `error`. When `_ring_enabled()` is false, the ring side fails with the disabled text without calling `_ring_search`/`_ring_extract`. In `keyless` mode a ring failure returns `tool_error("<error>. SearXNG is not used in this mode (HERMES_WEB_SEARCH_MODE=keyless).")`.

- [ ] **Step 1: Write the failing tests**

A helper `run(mode, tool, args, inner_result, ring_result, enabled=True, refusal=None)` sets `os.environ["HERMES_WEB_SEARCH_MODE"]`, replaces the four seams with recorders, wraps an inner stub that records calls and returns `inner_result` (or raises it if it is an exception), and returns `(result, calls)` where `calls` is the ordered list of `"searxng"`/`"ring"`. Tests:

```python
def test_unwrapped_tools():
    inner = lambda args, **kw: "x"
    expect(wsm.wrap_handler("lightrag", "query_document", inner) is inner, "other server")
    expect(wsm.wrap_handler("searxng", "searxng_get_info", inner) is inner, "other tool")

def test_private_is_untouched():
    r, calls = run("private", "searxng_web_search", {"query": "q"}, err("boom"), RING_OK)
    expect(r == err("boom") and calls == ["searxng"], (r, calls))

def test_private_first_search():
    r, calls = run("private-first", "searxng_web_search", {"query": "q"}, EMPTY_WITH_ENGINES, RING_OK)
    expect(calls == ["searxng", "ring"] and "SearXNG was unavailable for this search" in r, r)
    r, calls = run("private-first", "searxng_web_search", {"query": "q"}, CLEAN_EMPTY, RING_OK)
    expect(calls == ["searxng"], "clean empty does not leak to the ring")
    r, calls = run("private-first", "searxng_web_search", {"query": "q"}, PAUSE_ERR, RING_FAIL)
    expect(json.loads(r)["error"].startswith(json.loads(PAUSE_ERR)["error"]) and "(fallback via the keyless web tier also failed:" in r, r)

def test_keyless_first_search():
    r, calls = run("keyless-first", "searxng_web_search", {"query": "q"}, SEARX_OK, RING_FAIL)
    expect(calls == ["ring", "searxng"] and "The keyless web tier was unavailable for this search" in r, r)
    r, calls = run("keyless-first", "searxng_web_search", {"query": "q"}, SEARX_OK, RING_EMPTY_OK)
    expect(calls == ["ring"], "ring success with zero results is not a failure")

def test_keyless_search():
    r, calls = run("keyless", "searxng_web_search", {"query": "q"}, SEARX_OK, RING_FAIL)
    expect(calls == ["ring"] and "SearXNG is not used in this mode" in r, r)

def test_page_reads():
    a = {"url": "https://registry.example.com/x"}
    r, calls = run("private-first", "web_url_read", a, FETCH_403, EXTRACT_OK)
    expect(calls == ["searxng", "ring"] and "Served by the keyless web tier." in r, r)
    r, calls = run("keyless-first", "web_url_read", a, PAGE_OK, EXTRACT_ERR)
    expect(calls == ["ring", "searxng"], calls)
    r, calls = run("keyless", "web_url_read", {"url": "file:///etc/passwd"}, PAGE_OK, EXTRACT_OK)
    expect("ring" not in calls, "non-http URL never reaches the ring")

def test_guards():
    r, calls = run("keyless", "web_url_read", {"url": "http://10.0.0.5/admin"}, PAGE_OK, EXTRACT_OK,
                   refusal=err("Blocked: URL targets a private or internal network address"))
    expect("ring" not in calls and "private or internal" in r, r)
    r, calls = run("keyless", "searxng_web_search", {"query": "q"}, SEARX_OK, RING_OK, enabled=False)
    expect("ring" not in calls and "keyless web tier disabled (web.keyless_fallback: false)" in r, r)

def test_inner_raises():
    r, calls = run("private-first", "searxng_web_search", {"query": "q"}, RuntimeError("down"), RING_OK)
    expect(calls == ["searxng", "ring"], calls)
    try:
        run("private", "searxng_web_search", {"query": "q"}, RuntimeError("down"), RING_OK)
        fail("private re-raises")
    except RuntimeError:
        pass

def test_bad_mode_is_private():
    r, calls = run("keyless-only", "searxng_web_search", {"query": "q"}, err("boom"), RING_OK)
    expect(r == err("boom") and calls == ["searxng"], (r, calls))
```

Define the fixtures (`EMPTY_WITH_ENGINES`, `CLEAN_EMPTY`, `PAUSE_ERR`, `SEARX_OK`, `PAGE_OK`, `FETCH_403`, `RING_OK`, `RING_FAIL`, `RING_EMPTY_OK`, `EXTRACT_OK`, `EXTRACT_ERR`) at the top from the Task 1 strings. `PAUSE_ERR` uses the pause text upstream's `_check_circuit_breaker` returns (`MCP server 'searxng' is unreachable after 3 consecutive failures. ...`).

- [ ] **Step 2: Run to verify it fails**

Run: `python3 scripts/test-web-search-mode.py`
Expected: FAIL, `wrap_handler` not defined.

- [ ] **Step 3: Implement `wrap_handler` and the four seams**

- [ ] **Step 4: Run to verify it passes**

Run: `python3 scripts/test-web-search-mode.py`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add compose/hermes/web_search_mode.py scripts/test-web-search-mode.py
git commit -m "Serve searxng tools from the keyless ring by HERMES_WEB_SEARCH_MODE"
```

### Task 3: Overlay patch, wiring and image test

**Files:**
- Create: `compose/hermes/patch-web-search-mode.py`
- Modify: `compose/hermes/05-patch-tool-calling.sh`, `docker-compose.yml`
- Test: `scripts/test-web-search-mode.py`

**Interfaces:**
- Consumes: `wrap_handler` from Task 2, installed as `agent.web_search_mode`.
- Produces: patched `tools/mcp_tool_handlers.py` whose `_make_tool_handler(...)` returns `agent.web_search_mode.wrap_handler(server_name, tool_name, _handler)`.

The patch follows `patch-blocked-fetch-hint.py`: `ROOT`/`BOOTSTRAP` from env (`HERMES_ROOT`, `WEB_SEARCH_MODE_BOOTSTRAP`), `TAG = "[patch-web-search-mode]"`, `MARKER = "# assistant-stack: web search mode"`. Anchor: the line `    return _handler\n` that directly follows `return _mcp_result_with_native_images(text, image_paths) if native_images and not _result_is_error(text) else text\n` inside `_make_tool_handler`; `NEEDED` includes `"def _make_tool_handler(server_name: str, tool_name: str, tool_timeout: float, *, native_images: bool = False):"`. Replace it with the marker and `return _wrap_web_search_mode(server_name, tool_name, _handler)`, where `_wrap_web_search_mode` imports `agent.web_search_mode.wrap_handler` and falls back to returning `_handler` with a `logger.warning` if the import fails. The anchor must occur exactly once in the joined two-line form.

- [ ] **Step 1: Write the failing tests**

Add `test_overlay_is_wired()` (runner line `run_patch /bootstrap/patch-web-search-mode.py` after the blocked-fetch line; both files mounted `:ro`; compose has `HERMES_WEB_SEARCH_MODE=${HERMES_WEB_SEARCH_MODE:-private}`). Add `IMAGE_DRIVER` that runs the patch twice (second prints `already applied`), asserts the marker count is 1, imports `tools.mcp_tool_handlers`, and checks with upstream's real module:
- `_make_tool_handler("searxng", "searxng_web_search", 5)` returns a wrapper (not upstream's `_handler`: compare `__qualname__`), and `_make_tool_handler("lightrag", "query_document", 5).__qualname__` is upstream's.
- With `HERMES_WEB_SEARCH_MODE=keyless` and `plugins.web.keyless_mcp.search_with_failover` monkeypatched to a stub, calling the wrapper returns the rendered stub result without touching MCP.
- `agent.web_search_mode._url_refusal("http://10.0.0.5/")` returns a refusal and `_url_refusal("https://example.com/")` returns None (real upstream guards; `is_safe_url` does DNS for `example.com`, so if the image driver has no network, assert only the private case).
- A copy of `mcp_tool_handlers.py` with the anchor removed makes the patch exit non-zero with `expected code missing`.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 scripts/test-web-search-mode.py --image`
Expected: FAIL on `cont-init runs the patch`.

- [ ] **Step 3: Write the patch and wiring**

- [ ] **Step 4: Run to verify it passes**

Run: `python3 scripts/test-web-search-mode.py --image`
Expected: `ok` then `image ok`

Also run the neighbouring overlay tests, which share `05-patch-tool-calling.sh` and the searxng tool names:
`python3 scripts/test-blocked-fetch-hint.py --image && python3 scripts/test-searxng-pause-browser.py --image`
Expected: both end with `image ok`.

- [ ] **Step 5: Commit**

```bash
git add compose/hermes/patch-web-search-mode.py compose/hermes/05-patch-tool-calling.sh docker-compose.yml scripts/test-web-search-mode.py
git commit -m "Wire the web search mode overlay into Hermes cont-init"
```

### Task 4: Doctor check and docs

**Files:**
- Modify: `scripts/doctor.sh`, `docs/searxng.md`, `docs/hermes.md`, `.env.example`, `CHANGELOG.md`

- [ ] **Step 1: Doctor**

In `scripts/doctor.sh`, near the other Hermes checks, read `env_get HERMES_WEB_SEARCH_MODE private`; when it is not one of the four values (after trimming and lowercasing), `warn "HERMES_WEB_SEARCH_MODE=<value> is not private, private-first, keyless-first or keyless; Hermes uses private"`.

Verify: `HERMES_WEB_SEARCH_MODE=bogus` in a scratch copy is awkward because `env_get` reads `.env`; instead run `bash -n scripts/doctor.sh` and `make doctor` on the real `.env` (expected: no new WARN, since the key is unset).

- [ ] **Step 2: Docs**

- `docs/searxng.md`: a `## Search modes` section after `## Agent routing (Hermes / WebUI)`, written per the spec's Documentation section: the four-row table, how to switch (set the value in `.env`, then `docker compose up -d hermes`), the privacy paragraph naming Exa, Parallel, Firecrawl and Keenable, that the gate's pacing and cache cover only the SearXNG side, and that OpenCode, GPT Researcher and n8n are not affected. Update the Agent routing paragraph's claim that native search is the only alternative, if it implies that.
- `docs/hermes.md`: overlay table row for `compose/hermes/patch-web-search-mode.py` with `Test: scripts/test-web-search-mode.py`; in the web routing paragraph (line ~394), one sentence that the searxng tools may be served by the keyless ring depending on `HERMES_WEB_SEARCH_MODE` (link to the new section).
- `.env.example`: near `WEB_SEARCH_URL` (line ~590), a commented `# HERMES_WEB_SEARCH_MODE=private` with the four values and one line saying the keyless modes send chat queries and URLs to Exa, Parallel, Firecrawl and Keenable.
- `CHANGELOG.md`: under `## Unreleased`, add `### New optional .env keys` (if the section is absent) with `HERMES_WEB_SEARCH_MODE`, and a `### Changes` bullet. State that the default keeps today's behaviour.

- [ ] **Step 3: Commit**

```bash
git add scripts/doctor.sh docs/searxng.md docs/hermes.md .env.example CHANGELOG.md
git commit -m "Document HERMES_WEB_SEARCH_MODE and check it in make doctor"
```

### Task 5: Verify in the running stack

No code unless a check fails.

- [ ] **Step 1: Default mode unchanged.** From the worktree, `docker compose up -d hermes` (the worktree's compose file mounts its own `compose/hermes`; confirm with `docker inspect` that `/bootstrap/patch-web-search-mode.py` comes from the worktree path). Check `docker compose logs hermes | grep "patch-web-search-mode"` shows `patched` and the gateway reaches healthy (`docker compose ps hermes`).
- [ ] **Step 2: The gateway sees the variable** (Review Focus 1 and 2). Run, as the gateway's user, `docker exec -u hermes hermes sh -c 'tr "\0" "\n" < /proc/$(pgrep -f "gateway run --replace")/environ | grep HERMES_WEB_SEARCH_MODE'`. Expected: `HERMES_WEB_SEARCH_MODE=private`. Then check whether any `data/hermes/**/.env` sets the key (`grep -l` on names only); if Hermes loads profile env into `os.environ` and could override, add one sentence to `docs/searxng.md` saying so.
- [ ] **Step 3: Tests once before merge.** The Makefile has no test target. Run `python3 scripts/test-web-search-mode.py --image`, `python3 scripts/test-blocked-fetch-hint.py --image` and `python3 scripts/test-searxng-pause-browser.py --image` in one background call.
- [ ] **Step 4: Live ring check, only after Keith says go.** No chat session is used, since WebUI transcripts are session content. In the running container, as the gateway's user, run a one-off Python driver that imports `agent.web_search_mode`, wraps a stub inner handler that returns `SEARX_OK` for `searxng_web_search` and `PAGE_OK` for `web_url_read`, and calls each wrapper once with `HERMES_WEB_SEARCH_MODE=keyless` and once with `keyless-first`: one search for "SearXNG documentation" and one read of `https://docs.searxng.org/`. Pass: each ring-served result starts with `Served by the keyless web tier` and has at least one `URL:` line or page text. Report pass/fail per mode and which vendor answered.
