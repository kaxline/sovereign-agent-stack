#!/usr/bin/env python3
"""Checks the web-browser CDP gateway: the method rules on the host, then the
proxy itself inside the image (docs/contracts/browser.md).

The host part imports compose/web-browser/gateway/rules.py, which is stdlib
only. The proxy part needs websockets, so it runs inside
${IMAGE_PREFIX:-assistant}-web-browser:local against a fake CDP upstream, and
skips with a line when the image is absent (make web-browser-build).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "compose" / "web-browser"))

from testkit import Checks  # noqa: E402

from gateway import rules  # noqa: E402

DIALOG = "http://hermes-dialog-bridge.invalid/*"  # the pattern Hermes installs


def host_checks(checks: Checks) -> None:
    def verdict(role, method, params=None):
        return rules.verdict(role, method, params)[0]

    checks.expect(rules.CLIENTS == {"stack": rules.AGENT, "credentials": rules.CREDENTIALS},
                  f"clients are stack (agent) and credentials, got {rules.CLIENTS}")

    # --- the agent client: the contract's refused list, everything else through
    for method in ("Runtime.evaluate", "Page.enable", "Target.attachToTarget", "DOM.getDocument",
                   "Network.enable", "Network.getResponseBody", "IO.close", "Input.dispatchKeyEvent"):
        checks.expect(verdict(rules.AGENT, method) == rules.ALLOW, f"agent may call {method}")
    for method in ("Network.getAllCookies", "Network.getCookies", "Network.setCookie",
                   "Network.setCookies", "Network.deleteCookies", "Network.clearBrowserCookies",
                   "Storage.getCookies", "Storage.setCookies", "Storage.clearCookies",
                   "DOMStorage.getDOMStorageItems", "DOMStorage.enable", "IndexedDB.requestData",
                   "CacheStorage.requestCacheNames", "IO.read",
                   "Target.exposeDevToolsProtocol", "Target.attachToBrowserTarget"):
        checks.expect(verdict(rules.AGENT, method) == rules.REFUSE, f"agent may not call {method}")

    # Fetch: the dialog-bridge pattern only, auth interception off.
    for pattern in (DIALOG, "http://hermes-dialog-bridge.invalid/", DIALOG.rstrip("*") + "dialog"):
        checks.expect(verdict(rules.AGENT, "Fetch.enable",
                              {"patterns": [{"urlPattern": pattern, "requestStage": "Request"}],
                               "handleAuthRequests": False}) == rules.ALLOW,
                      f"Fetch.enable passes for the dialog-bridge pattern {pattern!r}")
    for pattern in ("http://hermes-dialog-bridge.invalid.evil.example/*", "http://*", 7):
        checks.expect(verdict(rules.AGENT, "Fetch.enable",
                              {"patterns": [{"urlPattern": pattern}]}) == rules.REFUSE,
                      f"Fetch.enable is refused for the pattern {pattern!r}")
    checks.expect(verdict(rules.AGENT, "Fetch.enable",
                          {"patterns": [{"urlPattern": DIALOG}], "handleAuthRequests": True}) == rules.REFUSE,
                  "Fetch.enable with auth interception is refused")
    checks.expect(verdict(rules.AGENT, "Fetch.enable",
                          {"patterns": [{"urlPattern": DIALOG}, {"urlPattern": "https://*"}]}) == rules.REFUSE,
                  "Fetch.enable with a second pattern is refused")
    checks.expect(verdict(rules.AGENT, "Fetch.enable", {}) == rules.REFUSE,
                  "Fetch.enable with no pattern is refused (every request pauses)")
    for method in ("Fetch.disable", "Fetch.continueRequest", "Fetch.fulfillRequest", "Fetch.failRequest"):
        checks.expect(verdict(rules.AGENT, method) == rules.ALLOW, f"{method} answers a paused request")
    for method in ("Fetch.continueWithAuth", "Fetch.getResponseBody", "Fetch.takeResponseBodyAsStream"):
        checks.expect(verdict(rules.AGENT, method) == rules.REFUSE, f"{method} is refused")

    # Browser.close is answered by the gateway, not forwarded.
    checks.expect(verdict(rules.AGENT, "Browser.close") == rules.ANSWER, "Browser.close is answered here")
    checks.expect(verdict(rules.CREDENTIALS, "Browser.close") == rules.ANSWER,
                  "Browser.close is answered here for the credential client too")

    # --- the URL rule, on both navigation methods
    for method in ("Page.navigate", "Target.createTarget"):
        for url in ("file:///etc/passwd", "FILE:///etc/passwd", "  file:///etc/passwd",
                    "chrome://settings", "chrome-extension://abc/page.html", "devtools://devtools/x",
                    "chrome-untrusted://x", "view-source:file:///etc/passwd"):
            checks.expect(verdict(rules.AGENT, method, {"url": url}) == rules.REFUSE,
                          f"{method} to {url!r} is refused")
        for url in ("https://example.com/login", "http://example.com/", "about:blank", ""):
            checks.expect(verdict(rules.AGENT, method, {"url": url}) == rules.ALLOW,
                          f"{method} to {url!r} passes")
        checks.expect(verdict(rules.AGENT, method, {"url": 7}) == rules.REFUSE,
                      f"{method} with a non-string url is refused")

    # --- the credential client: an allowlist, nothing else
    for method in ("Target.attachToTarget", "Target.detachFromTarget", "Target.getTargetInfo",
                   "Runtime.evaluate", "Runtime.callFunctionOn", "Network.setCookies", "Page.reload"):
        checks.expect(verdict(rules.CREDENTIALS, method) == rules.ALLOW,
                      f"the credential client may call {method}")
    for method in ("Page.navigate", "Target.createTarget", "Network.getAllCookies", "DOM.getDocument",
                   "Fetch.enable", "Target.attachToBrowserTarget", "Input.dispatchKeyEvent"):
        checks.expect(verdict(rules.CREDENTIALS, method) == rules.REFUSE,
                      f"the credential client may not call {method}")

    # An unknown role reaches nothing.
    checks.expect(verdict("viewer", "Runtime.evaluate") == rules.REFUSE, "an unknown role calls nothing")


IMAGE_DRIVER = r'''
import asyncio
import json
import sys

sys.path.insert(0, "/app")

import websockets

from gateway import rules
from gateway.server import Gateway

TOKENS = {"stack": "a" * 64, "credentials": "c" * 64}
seen = []


async def upstream(ws, path):
    async for raw in ws:
        msg = json.loads(raw)
        seen.append(msg.get("method"))
        await ws.send(json.dumps({"id": msg.get("id"), "result": {"saw": msg.get("method")}}))


def fail(msg):
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


async def call(ws, method, params=None, ident=1):
    body = {"id": ident, "method": method}
    if params is not None:
        body["params"] = params
    await ws.send(json.dumps(body))
    return json.loads(await asyncio.wait_for(ws.recv(), 5))


async def status_of(url, **kwargs):
    try:
        async with websockets.connect(url, **kwargs):
            return 101
    except websockets.InvalidStatusCode as exc:
        return exc.status_code
    except Exception as exc:  # the version's name for the same refusal
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code is None:
            raise
        return code


async def main():
    up = await websockets.serve(upstream, "127.0.0.1", 0)
    up_port = up.sockets[0].getsockname()[1]
    gw = Gateway(upstream_url=f"ws://127.0.0.1:{up_port}/devtools/browser/upstream", tokens=TOKENS,
                 host="127.0.0.1", port=0)
    port = await gw.start()
    base = f"ws://127.0.0.1:{port}/devtools/browser"

    # --- authentication
    for url, why in ((f"{base}/stack", "no token"),
                     (f"{base}/stack?token=", "empty token"),
                     (f"{base}/stack?token={'b' * 64}", "another client's shape of token"),
                     (f"{base}/credentials?token={TOKENS['stack']}", "the agent token on the credential path"),
                     (f"{base}/stack?token={TOKENS['stack']}x", "a token with a suffix")):
        code = await status_of(url)
        if code != 401:
            fail(f"{why} should be 401, got {code}")
    for url in (f"{base}/boundary?token={TOKENS['stack']}", f"/json/version", f"{base}"):
        code = await status_of(url if url.startswith("ws") else f"ws://127.0.0.1:{port}{url}")
        if code != 404:
            fail(f"an unknown path should be 404, got {code}")
    code = await status_of(f"{base}/stack?token={TOKENS['stack']}", origin="https://example.com")
    if code != 403:
        fail(f"an Origin header should be 403, got {code}")

    # --- forwarding, refusals and Browser.close
    async with websockets.connect(f"{base}/stack?token={TOKENS['stack']}") as agent:
        seen.clear()
        reply = await call(agent, "Runtime.evaluate", {"expression": "1"})
        if reply.get("result", {}).get("saw") != "Runtime.evaluate":
            fail(f"an allowed call should reach the browser, got {reply}")
        refused = await call(agent, "Network.getAllCookies", None, 2)
        if "error" not in refused or refused.get("id") != 2:
            fail(f"a refused call should answer with a CDP error, got {refused}")
        if "Network.getAllCookies" in seen:
            fail("a refused call must not reach the browser")
        nav = await call(agent, "Page.navigate", {"url": "file:///etc/passwd"}, 3)
        if "error" not in nav or "Page.navigate" in seen:
            fail(f"a file: navigation should be refused here, got {nav} {seen}")
        ok = await call(agent, "Page.navigate", {"url": "https://example.com/"}, 4)
        if ok.get("result", {}).get("saw") != "Page.navigate":
            fail(f"an http navigation should pass, got {ok}")

        # A second client at the same time, as the broker is beside the driver.
        async with websockets.connect(f"{base}/credentials?token={TOKENS['credentials']}") as cred:
            fill = await call(cred, "Runtime.callFunctionOn", {"objectId": "1"}, 5)
            if fill.get("result", {}).get("saw") != "Runtime.callFunctionOn":
                fail(f"the credential client should reach the browser, got {fill}")
            refused = await call(cred, "Page.navigate", {"url": "https://example.com/"}, 6)
            if "error" not in refused:
                fail(f"the credential client may not navigate, got {refused}")

            closed = await call(cred, "Browser.close", None, 7)
            if "result" not in closed:
                fail(f"Browser.close should answer with a result, got {closed}")
            try:
                await asyncio.wait_for(cred.recv(), 5)
            except websockets.ConnectionClosed:
                pass
            except asyncio.TimeoutError:
                fail("Browser.close should close that client's connection")
        if "Browser.close" in seen:
            fail("Browser.close must not reach the browser")
        # The other client, and the browser, are still there.
        still = await call(agent, "Runtime.evaluate", {"expression": "2"}, 8)
        if still.get("result", {}).get("saw") != "Runtime.evaluate":
            fail(f"the agent client should survive another client's Browser.close, got {still}")

    await gw.close()
    up.close()
    await up.wait_closed()
    print("image ok")


asyncio.run(main())
'''


def image_name() -> str:
    return f"{os.environ.get('IMAGE_PREFIX') or 'assistant'}-web-browser:local"


def image_checks(checks: Checks, argv: list[str]) -> None:
    name = image_name()
    required = "--image" in argv or os.environ.get("WEB_BROWSER_IMAGE_REQUIRED") == "1"
    if "--host-only" in argv:
        return
    if not shutil.which("docker") or subprocess.run(["docker", "image", "inspect", name],
                                                    capture_output=True).returncode != 0:
        if required:
            checks.fail(f"image checks: {name} is not built (make web-browser-build)")
        else:
            print(f"skip image checks ({name} not present; make web-browser-build)")
        return
    out = subprocess.run(["docker", "run", "--rm", "-i", "--network", "none",
                          "--entrypoint", "/usr/bin/python3", name, "-c", IMAGE_DRIVER],
                         capture_output=True, text=True)
    if out.returncode != 0 or "image ok" not in out.stdout:
        checks.fail(f"image checks ({name}):\n{out.stdout}{out.stderr}")
    else:
        print(f"image ok ({name})")


def main() -> None:
    checks = Checks()
    host_checks(checks)
    image_checks(checks, sys.argv[1:])
    checks.finish("ok web-browser gateway")


if __name__ == "__main__":
    main()
