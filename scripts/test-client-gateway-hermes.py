#!/usr/bin/env python3
"""Client gateway over a scripted Hermes (tasks 1.4–1.7): upstream login and
tickets, sessions (create with systemPrompt, cwd and mcpServers; list; load with
replay; resume; delete), prompt turns and their stream mapping, queued
prompts, cancel, approvals, clarify, the error table, a reaped runtime id, an
upstream drop mid-turn (reason runtime_restarted), and no prompt text in the
gateway's logs. Every frame validates against the ACP schema. Runs inside the
pinned Hermes image against scripts/lib/fake_tui_gateway.py."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import client_gateway_testkit as kit  # noqa: E402
from testkit import Checks  # noqa: E402

NS = "io.github.kaxline.stack"
SENTINEL = "SENTINEL-prompt-7f3a"
MCP = [
    {"type": "http", "name": "boundary", "url": "http://127.0.0.1:9/mcp", "headers": []},
    {"type": "http", "name": "calendar-mac", "url": "http://127.0.0.1:9/cal", "headers": []},
]


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def updates_of(cl, sid: str, kind: str) -> list[dict]:
    return [u["update"] for u in cl.updates if u["sessionId"] == sid and u["update"]["sessionUpdate"] == kind]


def text_of(cl, sid: str, kind: str) -> str:
    return "".join(u["content"]["text"] for u in updates_of(cl, sid, kind))


async def prompt(cl, sid: str, text: str, **extra):
    blocks = [{"type": "text", "text": text}, *extra.pop("blocks", [])]
    return await cl.request("session/prompt", {"sessionId": sid, "prompt": blocks}, timeout=20)


async def expect_error(c: Checks, coro, code: int, reason: str, what: str) -> None:
    from acp_client import RpcError

    try:
        await coro
        c.fail(f"{what}: no error")
    except RpcError as exc:
        c.expect((exc.code, exc.reason) == (code, reason), f"{what}: got {exc.code} {exc.reason}, want {code} {reason}")


async def checks(c: Checks) -> None:
    from client_gateway.auth import Settings
    from client_gateway.hermes.backend import HermesBackend
    from fake_tui_gateway import PASSWORD, USER, FakeTuiGateway

    capture = LogCapture()
    logging.getLogger().addHandler(capture)
    logging.getLogger().setLevel(logging.DEBUG)

    fake = FakeTuiGateway()
    hermes_url = await fake.start()
    settings = Settings(token=kit.TOKEN, hermes_url=hermes_url, dashboard_user=USER, dashboard_password=PASSWORD)
    backend = HermesBackend(settings)
    async with kit.serve(backend) as url:
        await asyncio.wait_for(backend.upstream.connected.wait(), 10)
        c.expect(fake.methods()[:1] == ["client.capabilities"], f"capabilities first: {fake.methods()[:3]}")
        c.expect(fake.calls[0][1] == {"server_requests": True}, "server_requests advertised")

        # Identities: profiles.list decides the agent endpoints.
        c.expect(await kit.upgrade_status(url, "/acp/agents/alpha") == 101, "alpha endpoint exists")
        c.expect(await kit.upgrade_status(url, "/acp/agents/ghost") == 404, "missing profile gives 404")
        c.expect(await kit.upgrade_status(url, "/acp/agents/default") == 404, "the default profile is /acp, not an agent")

        a = await kit.client(url)
        await a.initialize()

        # session/new: systemPrompt → context, cwd → session.cwd.set, mcpServers checked by name.
        res = await a.request("session/new", {
            "cwd": "/workspace", "mcpServers": MCP,
            "systemPrompt": {"mode": "append", "content": [{"type": "text", "text": "You are in a DM."}]},
        })
        sid = res["sessionId"]
        fs = fake.sessions.get(sid)
        c.expect(fs is not None, f"sessionId is the stored id: {sid}")
        if fs is None:
            return
        c.expect(fs.context == "You are in a DM." and fs.source == "boundary", f"context and source: {fs}")
        c.expect(fs.cwd == "/workspace", f"cwd set: {fs.cwd}")
        mcp = res["_meta"][NS]["mcpServers"]
        c.expect(mcp["applied"] == ["boundary"], f"registered server applied: {mcp}")
        c.expect(mcp["skipped"] == [{"name": "calendar-mac", "reason": "mcp_server_not_registered"}], f"skipped: {mcp}")
        await expect_error(c, a.request("session/new", {
            "cwd": "/", "mcpServers": [], "systemPrompt": {"mode": "override", "content": [{"type": "text", "text": "x"}]},
        }), -32602, "system_prompt_mode_unsupported", "override mode")
        await expect_error(c, a.request("session/new", {
            "cwd": "/", "mcpServers": [], "_meta": {NS: {"historyBudget": -1}},
        }), -32602, "history_budget_invalid", "negative history budget")

        # A plain turn: thought and message chunks, usage, end_turn.
        r = await prompt(a, sid, f"hi {SENTINEL}")
        c.expect(r["stopReason"] == "end_turn", f"end_turn: {r}")
        c.expect(text_of(a, sid, "agent_message_chunk") == "Hello world", f"message text: {text_of(a, sid, 'agent_message_chunk')!r}")
        c.expect(text_of(a, sid, "agent_thought_chunk") == "thinking", "thought chunk")
        usage = updates_of(a, sid, "usage_update")
        c.expect(bool(usage) and usage[-1]["used"] == 15 and usage[-1]["size"] == 32000, f"usage_update: {usage[-1:]}")
        c.expect(r.get("_meta", {}).get(NS, {}).get("usage", {}).get("output") == 5, f"usage in _meta: {r}")
        msg_ids = {u.get("messageId") for u in updates_of(a, sid, "agent_message_chunk")}
        c.expect(len(msg_ids) == 1 and None not in msg_ids, f"chunks share a messageId: {msg_ids}")

        # Back to back: Hermes still reports the first turn running for a moment
        # after message.complete; the second prompt must queue, not interrupt.
        a.updates.clear()
        r = await prompt(a, sid, "hi twice")
        c.expect(r["stopReason"] == "end_turn" and fake.busy_interrupts == 0,
                 f"back-to-back prompt queued, not redirected: {r} busy_interrupts={fake.busy_interrupts}")
        c.expect(not r.get("_meta", {}).get(NS, {}).get("queued"), "not reported queued (nothing ahead in the gateway)")

        # Tool calls.
        a.updates.clear()
        await prompt(a, sid, "tool")
        calls = updates_of(a, sid, "tool_call")
        done = updates_of(a, sid, "tool_call_update")
        c.expect(len(calls) == 1 and calls[0]["toolCallId"] == "call_1" and calls[0]["kind"] == "execute", f"tool_call: {calls}")
        c.expect(calls and calls[0].get("rawInput") == {"command": "ls /tmp"}, "rawInput")
        c.expect(len(done) == 1 and done[0]["status"] == "completed", f"tool_call_update: {done}")
        c.expect(done and done[0]["_meta"][NS]["durationS"] == 0.2, "duration in _meta")
        gen = [n for n in list(a.notifications._queue) if n["method"] == f"_{NS}/tool.generating"]
        c.expect(len(gen) == 1 and gen[0]["params"]["name"] == "terminal", f"tool.generating notification: {gen}")

        # Non-streaming reply: the final text arrives as one chunk.
        a.updates.clear()
        await prompt(a, sid, "nostream")
        c.expect(text_of(a, sid, "agent_message_chunk") == "Hello world", "nostream text from message.complete")

        # Approval → session/request_permission; "allow for session" answers choice=session.
        seen: list[dict] = []

        async def allow_session(p):
            seen.append(p)
            return {"outcome": {"outcome": "selected", "optionId": "allow_session"}}

        a.handlers["session/request_permission"] = allow_session
        await prompt(a, sid, "approve")
        c.expect(fake.answers[-1] == ("approval", {"choice": "session"}), f"approval answer: {fake.answers[-1:]}")
        if seen:
            opts = {o["optionId"]: o for o in seen[0]["options"]}
            c.expect(list(opts) == ["allow_once", "allow_session", "allow_always", "deny"], f"options: {list(opts)}")
            c.expect(opts["allow_session"]["kind"] == "allow_always" and opts["allow_session"]["_meta"][NS]["scope"] == "session",
                     "allow_session shape")
            raw = seen[0]["toolCall"]["rawInput"]
            c.expect(raw.get("command") == "rm -rf /tmp/x" and raw.get("pattern_key") == "rm", f"rawInput: {raw}")
        else:
            c.fail("no permission request reached the client")

        # Clarify → form elicitation.
        async def pick_blue(p):
            c.expect(p["mode"] == "form" and p["requestedSchema"]["properties"]["q1"]["enum"] == ["red", "blue"], f"form: {p}")
            return {"action": "accept", "content": {"q1": "blue"}}

        a.handlers["elicitation/create"] = pick_blue
        await prompt(a, sid, "clarify")
        c.expect(fake.answers[-1] == ("clarify", {"answers": {"q1": "blue"}, "outcome": "submitted"}), f"clarify: {fake.answers[-1:]}")

        # Queued: a prompt sent during a turn is queued and says so.
        async def slow_pick(p):
            await asyncio.sleep(0.5)
            return {"action": "accept", "content": {"q1": "red"}}

        a.handlers["elicitation/create"] = slow_pick
        _, first = a.start_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "clarify"}]})
        await asyncio.sleep(0.2)
        second = await prompt(a, sid, "hi again")
        r1 = await asyncio.wait_for(first, 10)
        c.expect(r1["stopReason"] == "end_turn" and not r1.get("_meta", {}).get(NS, {}).get("queued"), f"first: {r1}")
        c.expect(second.get("_meta", {}).get(NS, {}).get("queued") is True, f"second is queued: {second}")
        c.expect(("prompt.submit", {"session_id": fake.calls[-1][1].get("session_id"), "text": "hi again", "queued": True})
                 in fake.calls, "queued: true sent upstream")

        # Cancel a running turn.
        _, hang = a.start_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "hang"}]})
        await asyncio.sleep(0.3)
        await a.notify("session/cancel", {"sessionId": sid})
        r = await asyncio.wait_for(hang, 5)
        c.expect(r["stopReason"] == "cancelled", f"cancelled: {r}")

        # Errors: a failed turn, and over-long turn context.
        await expect_error(c, prompt(a, sid, "fail"), -32603, "turn_failed", "failed turn")
        ctx = {"type": "resource", "resource": {"uri": "stack:context", "text": "x" * 16001, "mimeType": "text/plain"}}
        await expect_error(c, prompt(a, sid, "hi", blocks=[ctx]), -32602, "context_invalid", "long context")
        ctx["resource"]["text"] = "In the #general room."
        await prompt(a, sid, "hi", blocks=[ctx])
        submitted = [p for m, p in fake.calls if m == "prompt.submit"][-1]
        c.expect(submitted.get("context") == "In the #general room." and submitted.get("text") == "hi", f"context sent: {submitted}")

        # A reaped runtime id is resumed transparently.
        fake.reap(sid)
        before = fake.methods().count("session.resume")
        r = await prompt(a, sid, "hi")
        c.expect(r["stopReason"] == "end_turn" and fake.methods().count("session.resume") == before + 1, "resume after 4001")

        # List, then load from a second client: replay with messageIds.
        lst = await a.request("session/list", {})
        c.expect(sid in [s["sessionId"] for s in lst["sessions"]], f"listed: {lst}")
        b = await kit.client(url)
        await b.initialize()
        await b.request("session/load", {"sessionId": sid, "cwd": "/", "mcpServers": []})
        users = updates_of(b, sid, "user_message_chunk")
        c.expect(any(SENTINEL in u["content"]["text"] for u in users), "user rows replayed")
        c.expect(all(str(u.get("messageId", "")).startswith("row:") for u in users), "replayed rows carry messageId")
        c.expect(any(u["toolCallId"] == "call_1" for u in updates_of(b, sid, "tool_call")), "tool rows replayed")
        b.updates.clear()
        await b.request("session/resume", {"sessionId": sid, "cwd": "/", "mcpServers": []})
        c.expect(not b.updates, "resume sends no history")
        await expect_error(c, b.request("session/load", {"sessionId": "20991231_000000_nope", "cwd": "/", "mcpServers": []}),
                           -32002, "session_not_found", "load of an unknown session")

        # Agent endpoint: its own profile, its own list.
        ag = await kit.client(url, "/acp/agents/alpha")
        await ag.initialize()
        asid = (await ag.request("session/new", {"cwd": "/", "mcpServers": []}))["sessionId"]
        c.expect(fake.sessions[asid].profile == "alpha", "agent session on its profile")
        c.expect([s["sessionId"] for s in (await ag.request("session/list", {}))["sessions"]] == [asid], "agent list")
        await expect_error(c, ag.request("session/load", {"sessionId": sid, "cwd": "/", "mcpServers": []}),
                           -32002, "session_not_found", "agent can't load the assistant's session")

        # Upstream drop mid-turn: runtime_restarted within 5 s, then loads work again.
        _, hang = a.start_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "hang"}]})
        await asyncio.sleep(0.3)
        t0 = time.monotonic()
        await fake.drop_all()
        await expect_error(c, asyncio.wait_for(hang, 5), -32603, "runtime_restarted", "drop mid-turn")
        c.expect(time.monotonic() - t0 < 5, "drop reported within 5 s")
        await asyncio.wait_for(backend.upstream.connected.wait(), 10)
        await a.request("session/load", {"sessionId": sid, "cwd": "/", "mcpServers": []})
        r = await prompt(a, sid, "hi")
        c.expect(r["stopReason"] == "end_turn", "turn after reconnect")
        c.expect(fake.logins >= 2, "a fresh login and ticket after the drop")

        # A bare error at admission ends the turn.
        await expect_error(c, prompt(a, sid, "admitfail"), -32603, "turn_failed", "admission refusal")

        # $/cancel_request on a running prompt: -32800, Hermes is interrupted, and the
        # next prompt gets its own reply, not the cancelled turn's.
        rid, hang = a.start_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "hang"}]})
        await asyncio.sleep(0.3)
        interrupts = fake.methods().count("session.interrupt")
        await a.notify("$/cancel_request", {"requestId": rid})
        try:
            await asyncio.wait_for(hang, 5)
            c.fail("$/cancel_request: prompt answered without an error")
        except Exception as exc:  # noqa: BLE001
            c.expect(getattr(exc, "code", None) == -32800, f"$/cancel_request gives -32800: {exc}")
        await asyncio.sleep(0.3)
        c.expect(fake.methods().count("session.interrupt") == interrupts + 1, "the abandoned turn was interrupted")
        a.updates.clear()
        r = await prompt(a, sid, "hi")
        c.expect(r["stopReason"] == "end_turn" and text_of(a, sid, "agent_message_chunk") == "Hello world",
                 f"next prompt after cancel_request: {r} {text_of(a, sid, 'agent_message_chunk')!r}")

        # A watcher that disconnects during a permission request doesn't stop the
        # other watcher from answering it.
        d = await kit.client(url)
        await d.initialize()
        await d.request("session/load", {"sessionId": sid, "cwd": "/", "mcpServers": [], "_meta": {NS: {"omitMessages": True}}})

        async def never(_p):
            await asyncio.sleep(3600)

        async def deny_later(_p):
            await asyncio.sleep(0.8)
            return {"outcome": {"outcome": "selected", "optionId": "deny"}}

        d.handlers["session/request_permission"] = never
        a.handlers["session/request_permission"] = deny_later
        _, appr = a.start_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "approve"}]})
        await asyncio.sleep(0.3)
        await d.close()
        r = await asyncio.wait_for(appr, 10)
        c.expect(fake.answers[-1] == ("approval", {"choice": "deny"}), f"surviving watcher answered: {fake.answers[-1:]}")

        # session/close during a running turn ends the prompt as cancelled.
        e = await kit.client(url)
        await e.initialize()
        esid = (await e.request("session/new", {"cwd": "/", "mcpServers": []}))["sessionId"]
        _, hang = e.start_request("session/prompt", {"sessionId": esid, "prompt": [{"type": "text", "text": "hang"}]})
        await asyncio.sleep(0.3)
        await e.request("session/close", {"sessionId": esid})
        r = await asyncio.wait_for(hang, 5)
        c.expect(r["stopReason"] == "cancelled", f"close ends the running turn: {r}")

        # A network blip (Hermes keeps running the turn): the client gets
        # runtime_restarted, and on the next call the leftover turn is interrupted
        # so the new prompt runs and gets its own reply.
        _, hang = a.start_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "hang"}]})
        await asyncio.sleep(0.3)
        await fake.blip()
        await expect_error(c, asyncio.wait_for(hang, 5), -32603, "runtime_restarted", "blip mid-turn")
        await asyncio.wait_for(backend.upstream.connected.wait(), 10)
        interrupts = fake.methods().count("session.interrupt")
        a.updates.clear()
        r = await prompt(a, sid, "hi")
        c.expect(fake.methods().count("session.interrupt") == interrupts + 1, "leftover turn interrupted after the blip")
        c.expect(r["stopReason"] == "end_turn" and text_of(a, sid, "agent_message_chunk") == "Hello world",
                 f"prompt after blip: {r} {text_of(a, sid, 'agent_message_chunk')!r}")

        # Delete.
        await b.request("session/close", {"sessionId": sid})
        await a.request("session/delete", {"sessionId": sid})
        c.expect(sid not in fake.sessions, "deleted upstream")

        for cl in (a, b, ag):
            c.expect(not cl.frame_errors, f"frames validate: {cl.frame_errors[:3]}")
            await cl.close()
    await fake.stop()

    leaked = [line for line in capture.lines if SENTINEL in line]
    c.expect(not leaked, f"prompt text reached the logs: {leaked[:1]}")


def main() -> None:
    c = Checks()
    if kit.IN_IMAGE:
        kit.in_image_paths()
        asyncio.run(checks(c))
        c.finish("ok client-gateway hermes (image)")
        return
    kit.run_in_image(__file__)


if __name__ == "__main__":
    main()
