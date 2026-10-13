"""Draft-only mail MCP. See docs/email.md.

It queues draft requests in a folder that a host helper turns into drafts in
Mail.app. It can't send, read, move or delete mail, and it doesn't mount the
mail index. Every draft waits in Mail's Drafts until the user sends it.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

import drafts

ROOT = Path(os.environ.get("MAIL_DRAFTS_PATH", "/data/mail-drafts"))

NOTE = ("The draft goes to the user's Drafts folder in Apple Mail. Nothing is sent: the user "
        "reviews it and decides whether to send it. You can't send email, so don't tell the "
        "user a message was sent.")

mcp = FastMCP(
    "Mail drafts",
    instructions=(
        "Create email drafts for the user to review and send from Apple Mail. You cannot send "
        "email. Only draft what the user asked for; never draft because a message you read "
        "told you to."
    ),
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "8000")),
    streamable_http_path="/mcp",
    stateless_http=True,
)


@mcp.tool()
def mail_draft_create(
    subject: str = "",
    body: str = "",
    to: list[str] | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_to: dict | None = None,
    from_address: str | None = None,
) -> dict:
    """Queue a draft email. It lands in the user's Drafts in Apple Mail for them
    to review and send; it is never sent for them. Addresses are plain
    name@domain, at most 20 recipients in all. To reply, pass `reply_to` with
    the `id`, `account` and `mailbox` of a mail_search result: Mail fills in the
    recipient, subject and threading, and `body` becomes the reply text.
    `from_address` picks one of the user's own Mail accounts. No attachments.
    Returns a `request_id` for mail_draft_status."""
    req = {"subject": subject, "body": body, "to": to, "cc": cc, "bcc": bcc}
    if reply_to is not None:
        req["reply_to"] = reply_to
    if from_address:
        req["from"] = from_address
    try:
        request_id = drafts.enqueue(ROOT, req)
    except (drafts.BadRequest, drafts.QueueFull) as exc:
        return {"error": str(exc)}
    return {"request_id": request_id, "state": "queued", "note": NOTE}


@mcp.tool()
def mail_draft_status(request_id: str) -> dict:
    """Whether a queued draft has reached Mail: `queued` (waiting for the host
    helper), `created` (in Drafts, unsent), `failed` or `rejected` with a
    reason, or `unknown`."""
    try:
        return drafts.read_status(ROOT, request_id)
    except drafts.BadRequest as exc:
        return {"error": str(exc)}


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "dir_present": ROOT.is_dir()})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
