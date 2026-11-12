#!/usr/bin/env python3
"""Client gateway connection loop (tasks 1.2, 1.3): initialize, unknown methods,
parse errors, $/cancel_request, the session methods against an in-memory
backend, fan-out to two clients and first-answer-wins permission requests.
Every frame the clients receive is validated against the vendored ACP schema.
Runs inside the pinned Hermes image."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import client_gateway_testkit as kit  # noqa: E402
from testkit import Checks  # noqa: E402

NS = "io.github.kaxline.stack"


async def expect_error(c: Checks, coro, code: int, reason: str | None, what: str) -> None:
    from acp_client import RpcError

    try:
        await coro
        c.fail(f"{what}: no error")
    except RpcError as exc:
        c.expect(exc.code == code, f"{what}: code {exc.code}, want {code}")
        if reason is not None:
            c.expect(exc.reason == reason, f"{what}: reason {exc.reason}, want {reason}")


async def checks(c: Checks) -> None:
    from fake_backend import FakeBackend

    backend = FakeBackend()
    async with kit.serve(backend) as url:
        a = await kit.client(url)
        init = await a.initialize()
        c.expect(init["protocolVersion"] == 1, f"protocolVersion 1: {init}")
        caps = init["agentCapabilities"]
        c.expect(caps["loadSession"] is True, "loadSession advertised")
        c.expect(caps["promptCapabilities"]["embeddedContext"] is True, "embeddedContext advertised")
        c.expect(caps["mcpCapabilities"]["http"] is True, "mcp http advertised")
        c.expect(set(caps["sessionCapabilities"]) >= {"list", "resume", "close", "delete"}, "session caps")
        c.expect(isinstance(caps["_meta"][NS]["extensions"], dict), "extensions listed under the namespace")
        c.expect(init["agentInfo"]["name"] == "hermes", f"agentInfo names the identity: {init['agentInfo']}")

        await expect_error(c, a.request("nope/method"), -32601, "method_not_found", "unknown method")
        await expect_error(c, a.request("session/new", {"cwd": 5}), -32602, "invalid_params", "bad cwd")
        await expect_error(
            c, a.request("session/prompt", {"sessionId": "zz", "prompt": []}), -32002, "session_not_open",
            "prompt on a session this connection hasn't opened",
        )

        sid = (await a.request("session/new", {"cwd": "/tmp", "mcpServers": []}))["sessionId"]
        r = await a.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]})
        c.expect(r["stopReason"] == "end_turn", f"prompt ends end_turn: {r}")
        c.expect(any(u["update"].get("content", {}).get("text") == "echo: hi" for u in a.updates), "chunk delivered")

        # $/cancel_request on a running prompt answers -32800.
        rid, task = a.start_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "wait"}]})
        await asyncio.sleep(0.2)
        await a.notify("$/cancel_request", {"requestId": rid})
        await expect_error(c, task, -32800, None, "$/cancel_request")

        # session/cancel reaches the backend; the turn ends cancelled.
        _, task = a.start_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "wait"}]})
        await asyncio.sleep(0.2)
        await a.notify("session/cancel", {"sessionId": sid})
        r = await asyncio.wait_for(task, 5)
        c.expect(r["stopReason"] == "cancelled", f"session/cancel ends the turn cancelled: {r}")
        c.expect(sid in backend.cancelled, "backend saw cancel")

        # Raw garbage gets a parse error and the socket stays usable.
        await a.send_raw("{not json")
        lst = await a.request("session/list", {})
        c.expect([s["sessionId"] for s in lst["sessions"]] == [sid], f"list: {lst}")

        # Two clients on one session: both get updates; the first permission answer wins
        # and the other client's request is cancelled.
        b = await kit.client(url)
        await b.initialize()
        await b.request("session/load", {"sessionId": sid, "cwd": "/tmp", "mcpServers": []})
        c.expect(any(u["update"].get("content", {}).get("text") == "echo: hi" for u in b.updates), "load replays history")

        async def slow(_p):
            await asyncio.sleep(5)
            return {"outcome": {"outcome": "selected", "optionId": "deny"}}

        async def fast(_p):
            return {"outcome": {"outcome": "selected", "optionId": "allow_once"}}

        a.handlers["session/request_permission"] = fast
        b.handlers["session/request_permission"] = slow
        before_b = len(b.updates)
        r = await a.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "ask twice"}]})
        c.expect(r["stopReason"] == "end_turn", f"ask turn ends: {r}")
        c.expect(backend.permission_answers[-1]["outcome"].get("optionId") == "allow_once", "first answer wins")
        c.expect(len(b.server_requests) == 1, "the second client was asked too")
        await asyncio.sleep(0.2)
        c.expect(len(b.cancelled_requests) == 1, f"the second client's request was cancelled: {b.cancelled_requests}")
        c.expect(len(b.updates) > before_b, "the second client got the turn's updates")

        # close detaches; delete removes it for everyone.
        await b.request("session/close", {"sessionId": sid})
        await a.request("session/delete", {"sessionId": sid})
        lst = await a.request("session/list", {})
        c.expect(lst["sessions"] == [], f"deleted session is gone: {lst}")

        # The agent endpoint sees its own sessions only.
        ag = await kit.client(url, "/acp/agents/alpha")
        init = await ag.initialize()
        c.expect(init["agentInfo"]["name"] == "alpha", "agent endpoint names the agent")
        c.expect((await ag.request("session/list", {}))["sessions"] == [], "agent list is separate")

        for cl in (a, b, ag):
            c.expect(not cl.frame_errors, f"frames validate: {cl.frame_errors[:3]}")
            await cl.close()


def main() -> None:
    c = Checks()
    if kit.IN_IMAGE:
        kit.in_image_paths()
        asyncio.run(checks(c))
        c.finish("ok client-gateway connection (image)")
        return
    kit.run_in_image(__file__)


if __name__ == "__main__":
    main()
