#!/usr/bin/env python3
"""Checks for HERMES_WEB_SEARCH_MODE (web_search_mode.py, patch-web-search-mode.py).

Host part is stdlib-only and replaces the ring and URL-guard seams with
recorders, so nothing leaves the machine. With the pinned Hermes image present
(or --image), the patch is applied in the image and the wrapped handler is
checked against upstream's real modules.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import web_search_mode as wsm  # noqa: E402
import hermes_image  # noqa: E402

UNAVAILABLE = "⚠️ Some search engines were unavailable: {}. Results may be incomplete."


def fail(msg: object) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: object) -> None:
    if not cond:
        fail(msg)


def err(text: str) -> str:
    return json.dumps({"error": text})


def res(text: str) -> str:
    return json.dumps({"result": text}, ensure_ascii=False)


def test_current_mode() -> None:
    expect(wsm.current_mode({}) == "private", "unset")
    expect(wsm.current_mode({"HERMES_WEB_SEARCH_MODE": ""}) == "private", "empty")
    for m in wsm.MODES:
        expect(wsm.current_mode({"HERMES_WEB_SEARCH_MODE": f" {m.upper()} "}) == m, m)
    expect(wsm.current_mode({"HERMES_WEB_SEARCH_MODE": "keyless-only"}) == "private", "bad value")


def test_search_failed() -> None:
    expect(wsm.search_failed(err("MCP server 'searxng' is unreachable after 3 consecutive failures.")), "tool error")
    expect(wsm.search_failed(res('No results found for "x".\n\n' + UNAVAILABLE.format("bing (CAPTCHA)"))),
           "empty + engines")
    busy = "search-gate (busy: the search is queued; ask again in a minute or two)"
    expect(wsm.search_failed(res('No results found for "x".\n\n' + UNAVAILABLE.format(busy))), "gate busy")
    expect(wsm.search_failed(res(json.dumps({"results": [], "unresponsive_engines": [["bing", "CAPTCHA"]]}))),
           "json empty + engines")
    expect(not wsm.search_failed(res('No results found for "x".')), "clean empty")
    partial = "Title: A\nDescription: b\nURL: https://a.example.com\n\n" + UNAVAILABLE.format("bing (CAPTCHA)")
    expect(not wsm.search_failed(res(partial)), "partial results")
    expect(not wsm.search_failed(res(json.dumps({"results": [{"url": "https://a.example.com"}]}))), "json results")


def test_fetch_failed() -> None:
    a = {"url": "https://registry.example.com/x"}
    expect(wsm.fetch_failed(a, err("\U0001F6AB Website Error (403): Access blocked (bot detection or geo-restriction)")),
           "403")
    expect(wsm.fetch_failed(a, err("\U0001F310 Network Error: fetch failed")), "tool error")
    expect(not wsm.fetch_failed(a, res("# Acme\n\n" + "text " * 50)), "good page")


def test_render() -> None:
    resp = {"success": True, "data": {"web": [{"title": "A", "url": "https://a.example.com",
                                               "description": "about a"}], "served_by": "parallel"}}
    text = json.loads(wsm.render_search(resp, {"query": "q", "time_range": "week"}))["result"]
    expect(text.startswith("Served by the keyless web tier (parallel)."), text)
    expect("Title: A\nDescription: about a\nURL: https://a.example.com" in text, text)
    expect("time_range" in text and "not applied" in text, "ignored filter note")
    plain = json.loads(wsm.render_search(resp, {"query": "q"}))["result"]
    expect("not applied" not in plain, plain)
    j = json.loads(json.loads(wsm.render_search(resp, {"query": "q", "response_format": "json"}))["result"])
    expect(j == {"results": [{"title": "A", "url": "https://a.example.com", "content": "about a"}],
                 "served_by": "parallel"}, j)
    page = {"url": "https://a.example.com", "title": "A", "content": "0123456789"}
    text = json.loads(wsm.render_extract(page, {"url": page["url"], "startChar": 2, "maxLength": 3}))["result"]
    expect(text.startswith("Served by the keyless web tier.") and text.endswith("234"), text)
    text = json.loads(wsm.render_extract(page, {"url": page["url"], "section": "Intro"}))["result"]
    expect("whole page" in text and "0123456789" in text, text)


EMPTY_WITH_ENGINES = res('No results found for "q".\n\n' + UNAVAILABLE.format("bing (CAPTCHA)"))
CLEAN_EMPTY = res('No results found for "q".')
PAUSE_ERR = err("MCP server 'searxng' is unreachable after 3 consecutive failures. Paused for 60 s.")
SEARX_OK = res("Title: S\nDescription: from searxng\nURL: https://s.example.com")
PAGE_OK = res("# Registry\n\n" + "plain fetch text " * 20)
FETCH_403 = err("\U0001F6AB Website Error (403): Access blocked (bot detection or geo-restriction)")
RING_OK = {"success": True, "data": {"web": [{"title": "R", "url": "https://r.example.com", "description": "ring"}]}}
RING_EMPTY_OK = {"success": True, "data": {"web": []}}
RING_FAIL = {"success": False, "error": "HTTP 429 (all keyless vendors unavailable: exa, parallel, firecrawl, keenable)"}
EXTRACT_OK = {"url": "https://registry.example.com/x", "title": "Registry", "content": "ring page text"}
EXTRACT_ERR = {"url": "https://registry.example.com/x", "title": "", "content": "", "error": "HTTP 403 from origin"}


def run(mode, tool, args, inner_result, ring_result, enabled=True, refusal=None):
    calls: list[str] = []

    def inner(a, **kw):
        calls.append("searxng")
        if isinstance(inner_result, Exception):
            raise inner_result
        return inner_result

    def ring_search(query, limit):
        calls.append("ring")
        return json.loads(json.dumps(ring_result))

    def ring_extract(url):
        calls.append("ring")
        return dict(ring_result)

    saved = {n: getattr(wsm, n) for n in ("_ring_search", "_ring_extract", "_ring_enabled", "_url_refusal")}
    wsm._ring_search, wsm._ring_extract = ring_search, ring_extract
    wsm._ring_enabled = lambda: enabled
    wsm._url_refusal = lambda url: refusal
    os.environ["HERMES_WEB_SEARCH_MODE"] = mode
    try:
        return wsm.wrap_handler("searxng", tool, inner)(dict(args)), calls
    finally:
        for n, f in saved.items():
            setattr(wsm, n, f)
        os.environ.pop("HERMES_WEB_SEARCH_MODE", None)


def test_unwrapped_tools() -> None:
    inner = lambda args, **kw: "x"  # noqa: E731
    expect(wsm.wrap_handler("lightrag", "query_document", inner) is inner, "other server")
    expect(wsm.wrap_handler("searxng", "searxng_get_info", inner) is inner, "other tool")


def test_private_is_untouched() -> None:
    r, calls = run("private", "searxng_web_search", {"query": "q"}, err("boom"), RING_OK)
    expect(r == err("boom") and calls == ["searxng"], (r, calls))
    r, calls = run("private", "web_url_read", {"url": "https://a.example.com"}, FETCH_403, EXTRACT_OK)
    expect(r == FETCH_403 and calls == ["searxng"], (r, calls))


def test_private_first_search() -> None:
    r, calls = run("private-first", "searxng_web_search", {"query": "q"}, EMPTY_WITH_ENGINES, RING_OK)
    expect(calls == ["searxng", "ring"] and "SearXNG was unavailable for this search" in r, r)
    expect("URL: https://r.example.com" in json.loads(r)["result"], r)
    r, calls = run("private-first", "searxng_web_search", {"query": "q"}, CLEAN_EMPTY, RING_OK)
    expect(calls == ["searxng"] and r == CLEAN_EMPTY, "clean empty does not leak to the ring")
    r, calls = run("private-first", "searxng_web_search", {"query": "q"}, SEARX_OK, RING_OK)
    expect(calls == ["searxng"] and r == SEARX_OK, "searxng success is returned as is")
    r, calls = run("private-first", "searxng_web_search", {"query": "q"}, PAUSE_ERR, RING_FAIL)
    e = json.loads(r)["error"]
    expect(e.startswith(json.loads(PAUSE_ERR)["error"]) and "(fallback via the keyless web tier also failed:" in e, r)


def test_keyless_first_search() -> None:
    r, calls = run("keyless-first", "searxng_web_search", {"query": "q"}, SEARX_OK, RING_FAIL)
    expect(calls == ["ring", "searxng"] and "The keyless web tier was unavailable for this search" in r, r)
    expect("URL: https://s.example.com" in json.loads(r)["result"], r)
    r, calls = run("keyless-first", "searxng_web_search", {"query": "q"}, SEARX_OK, RING_EMPTY_OK)
    expect(calls == ["ring"], "ring success with zero results is not a failure")
    r, calls = run("keyless-first", "searxng_web_search", {"query": "q"}, PAUSE_ERR, RING_FAIL)
    e = json.loads(r)["error"]
    expect(e.startswith(RING_FAIL["error"]) and "(fallback via SearXNG also failed:" in e, r)


def test_keyless_search() -> None:
    r, calls = run("keyless", "searxng_web_search", {"query": "q"}, SEARX_OK, RING_OK)
    expect(calls == ["ring"] and json.loads(r)["result"].startswith("Served by the keyless web tier (exa)."), r)
    r, calls = run("keyless", "searxng_web_search", {"query": "q"}, SEARX_OK, RING_FAIL)
    expect(calls == ["ring"] and "SearXNG is not used in this mode" in json.loads(r)["error"], r)


def test_page_reads() -> None:
    a = {"url": "https://registry.example.com/x"}
    r, calls = run("private-first", "web_url_read", a, FETCH_403, EXTRACT_OK)
    expect(calls == ["searxng", "ring"] and "Served by the keyless web tier." in r, r)
    expect("SearXNG was unavailable for this page read" in r and "ring page text" in r, r)
    r, calls = run("private-first", "web_url_read", a, PAGE_OK, EXTRACT_OK)
    expect(calls == ["searxng"] and r == PAGE_OK, "good fetch is returned as is")
    r, calls = run("keyless-first", "web_url_read", a, PAGE_OK, EXTRACT_ERR)
    expect(calls == ["ring", "searxng"] and "plain fetch text" in r, (calls, r))
    r, calls = run("keyless", "web_url_read", a, PAGE_OK, EXTRACT_ERR)
    expect(calls == ["ring"] and "HTTP 403 from origin" in json.loads(r)["error"], r)
    r, calls = run("keyless", "web_url_read", {"url": "file:///etc/passwd"}, PAGE_OK, EXTRACT_OK)
    expect("ring" not in calls and r == PAGE_OK, "non-http URL never reaches the ring")
    r, calls = run("private-first", "web_url_read", {}, FETCH_403, EXTRACT_OK)
    expect(calls == ["searxng"], "no URL never reaches the ring")


def test_guards() -> None:
    refusal = err("Blocked: URL targets a private or internal network address")
    r, calls = run("keyless", "web_url_read", {"url": "http://10.0.0.5/admin"}, PAGE_OK, EXTRACT_OK, refusal=refusal)
    expect("ring" not in calls and "private or internal" in r, r)
    r, calls = run("private-first", "web_url_read", {"url": "http://10.0.0.5/admin"}, FETCH_403, EXTRACT_OK,
                   refusal=refusal)
    expect(calls == ["searxng"] and "private or internal" in r, r)
    r, calls = run("keyless", "searxng_web_search", {"query": "q"}, SEARX_OK, RING_OK, enabled=False)
    expect("ring" not in calls and "keyless web tier disabled (web.keyless_fallback: false)" in r, r)


def test_inner_raises() -> None:
    r, calls = run("private-first", "searxng_web_search", {"query": "q"}, RuntimeError("down"), RING_OK)
    expect(calls == ["searxng", "ring"] and "down" in r, (calls, r))
    try:
        run("private", "searxng_web_search", {"query": "q"}, RuntimeError("down"), RING_OK)
        fail("private re-raises")
    except RuntimeError:
        pass


def test_bad_mode_is_private() -> None:
    r, calls = run("keyless-only", "searxng_web_search", {"query": "q"}, err("boom"), RING_OK)
    expect(r == err("boom") and calls == ["searxng"], (r, calls))


def test_overlay_is_wired() -> None:
    runner = (ROOT / "compose" / "hermes" / "05-patch-tool-calling.sh").read_text()
    line = "run_patch /bootstrap/patch-web-search-mode.py"
    expect(line in runner, "cont-init runs the patch")
    expect(runner.index(line) > runner.index("run_patch /bootstrap/patch-blocked-fetch-hint.py"),
           "runs after the blocked-fetch hint it imports")
    compose = (ROOT / "docker-compose.yml").read_text()
    for name in ("web_search_mode.py", "patch-web-search-mode.py"):
        expect(f"./compose/hermes/{name}:/bootstrap/{name}:ro" in compose, f"{name} is mounted")
    expect("- HERMES_WEB_SEARCH_MODE=${HERMES_WEB_SEARCH_MODE:-private}" in compose, "env reaches hermes")


IMAGE_DRIVER = r'''
import json, os, shutil, subprocess, sys, tempfile
for _ in range(2):
    out = subprocess.run([sys.executable, "/bootstrap/patch-web-search-mode.py"], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
assert "already applied" in out.stdout, out.stdout
src = open("/opt/hermes/tools/mcp_tool_handlers.py").read()
assert src.count("# assistant-stack: web search mode") == 1

sys.path.insert(0, "/opt/hermes")
from tools import mcp_tool_handlers as h
wrapped = h._make_tool_handler("searxng", "searxng_web_search", 5)
plain = h._make_tool_handler("lightrag", "query_document", 5)
assert "wrap_handler" in wrapped.__qualname__, wrapped.__qualname__
assert plain.__qualname__ == "_make_tool_handler.<locals>._handler", plain.__qualname__

import plugins.web.keyless_mcp as km
km.search_with_failover = lambda name, query, limit: {
    "success": True, "data": {"web": [{"title": "T", "url": "https://t.example.com", "description": "d"}]}}
km.keyless_enabled = lambda: True
os.environ["HERMES_WEB_SEARCH_MODE"] = "keyless"
out = json.loads(wrapped({"query": "q"}))
assert out["result"].startswith("Served by the keyless web tier (exa)."), out
assert "URL: https://t.example.com" in out["result"], out

from agent import web_search_mode as wsm
refusal = wsm._url_refusal("http://10.0.0.5/admin")
assert refusal and "private or internal" in refusal, refusal
assert "API key" in (wsm._url_refusal("https://x.example.com/?api_key=sk-abcdefghijklmnopqrstuvwxyz0123456789") or "")

tmp = tempfile.mkdtemp()
os.makedirs(f"{tmp}/agent"); os.makedirs(f"{tmp}/tools")
open(f"{tmp}/tools/mcp_tool_handlers.py", "w").write(src.replace("# assistant-stack: web search mode", "").replace(
    "    return _wrap_web_search_mode(server_name, tool_name, _handler)\n", "    return _handlerX\n"))
out = subprocess.run([sys.executable, "/bootstrap/patch-web-search-mode.py"], capture_output=True, text=True,
                     env={**os.environ, "HERMES_ROOT": tmp})
assert out.returncode != 0 and "expected code missing" in (out.stdout + out.stderr), out.stdout + out.stderr
os.write(1, b"image ok\n")
'''


def main() -> None:
    test_current_mode()
    test_search_failed()
    test_fetch_failed()
    test_render()
    test_unwrapped_tools()
    test_private_is_untouched()
    test_private_first_search()
    test_keyless_first_search()
    test_keyless_search()
    test_page_reads()
    test_guards()
    test_inner_raises()
    test_bad_mode_is_private()
    test_overlay_is_wired()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
