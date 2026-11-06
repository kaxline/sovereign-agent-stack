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


def main() -> None:
    test_current_mode()
    test_search_failed()
    test_fetch_failed()
    test_render()
    print("ok")


if __name__ == "__main__":
    main()
