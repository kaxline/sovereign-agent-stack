"""Turn queued draft requests into drafts. See docs/email.md.

The mail-drafts MCP container writes requests into <dir>/queue. This helper
runs on the host, checks each request again (it doesn't trust the
container), saves it as a draft, records the outcome in
<dir>/status/<id>.json and deletes the request. It never sends: the user
sends drafts themselves. It has no access to the mail index.

Drafts for Mail.app accounts go through mail_drafts.js, which reads no
message content. Drafts for the config's IMAP accounts are appended to the
account's Drafts folder (mail_imap_drafts.py); a reply reads five headers of
the original, never its body.

Output is counts only. Status files hold a state and a short reason, never
the draft's text or recipients.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import jxa

_DRAFTS_LIB = Path(__file__).resolve().parents[2] / "compose" / "mail-drafts-mcp"
if str(_DRAFTS_LIB) not in sys.path:
    sys.path.insert(0, str(_DRAFTS_LIB))

import drafts  # noqa: E402

SCRIPT = Path(__file__).with_name("mail_drafts.js")
STATUS_KEEP_DAYS = 30


def for_mail_app(request: dict) -> dict:
    """The request as mail_drafts.js takes it: reply_to names Mail's message id."""
    reply = request.get("reply_to")
    if not reply:
        return request
    return {**request, "reply_to": {"id": int(reply["source_id"]), "account": reply["account"],
                                    "mailbox": reply["mailbox"]}}


@dataclass
class Stats:
    created: int = 0
    failed: int = 0
    rejected: int = 0
    deferred: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class Helper:
    """Saves queued drafts through one outbox per account: Mail.app (the
    default) or, for the config's `source = "imap"` accounts, IMAP."""

    def __init__(self, root: Path, *, runner: jxa.Runner | None = None, open_windows: bool = False,
                 max_per_hour: int = 20, timeout: int = 120, imap_accounts: dict | None = None,
                 mail_app: bool = True, imap_connect=None) -> None:
        self.root = Path(root)
        self._runner = runner
        self._timeout = timeout
        self.open_windows = open_windows
        self.max_per_hour = max_per_hour
        self.imap_accounts = imap_accounts or {}  # {ID: (id, settings)}, from mail_imap_conn.imap_accounts
        self.mail_app = mail_app
        self.imap_connect = imap_connect

    @property
    def runner(self) -> jxa.Runner:
        if self._runner is None:
            self._runner = jxa.osascript_runner(SCRIPT, self._timeout)
        return self._runner

    def _outbox(self, request: dict) -> tuple[str, str | None] | str:
        """("imap", account id) or ("mail_app", None), or a reason to reject."""
        reply = request.get("reply_to")
        if reply:
            if reply["account"].upper() in self.imap_accounts:
                return "imap", reply["account"].upper()
            if not self.mail_app:
                return "reply_account_not_configured"
            if not reply["source_id"].isdigit():
                return "reply_message_not_found"
            return "mail_app", None
        sender = (request.get("from") or "").lower()
        if sender:
            for key, (_, settings) in self.imap_accounts.items():
                if sender in settings["addresses"]:
                    return "imap", key
        if self.mail_app:
            return "mail_app", None
        if not sender and len(self.imap_accounts) == 1:
            return "imap", next(iter(self.imap_accounts))
        return "from_not_an_account" if sender else "pass from_address to pick an account"

    def _save_imap(self, key: str, request: dict) -> str | None:
        """None when saved, else a short reason."""
        import mail_credentials
        import mail_imap_conn
        import mail_imap_drafts
        account_id, settings = self.imap_accounts[key]
        try:
            mail_imap_drafts.save_draft(account_id, settings, request,
                                        **({"connect": self.imap_connect} if self.imap_connect else {}))
        except mail_imap_drafts.DraftError as exc:
            return str(exc)
        except mail_imap_conn.AuthFailed:
            return "imap_login_refused"
        except mail_credentials.MissingPassword:
            return "imap_password_missing"
        except mail_imap_conn.ImapError:
            return "imap_unreachable"
        return None

    def _created_since(self, since: float) -> int:
        _, status = drafts.dirs(self.root)
        count = 0
        for path in status.glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if data.get("state") == "created" and data.get("at", 0) >= since:
                count += 1
        return count

    def _prune(self, now: float) -> None:
        _, status = drafts.dirs(self.root)
        cutoff = now - STATUS_KEEP_DAYS * 86400
        for path in status.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    def process(self, *, now: float | None = None) -> Stats:
        """One pass over the queue, oldest first. AutomationDenied leaves the queue as it is."""
        now = time.time() if now is None else now
        stats = Stats()
        self._prune(now)
        budget = self.max_per_hour - self._created_since(now - 3600)
        for path in drafts.pending(self.root):
            request_id = path.stem
            if budget <= 0:
                stats.deferred += 1
                continue
            try:
                raw = json.loads(path.read_text())
                clean = drafts.validate(raw)
                if clean.get("id") != request_id:
                    raise drafts.BadRequest("request id doesn't match its file name")
            except (ValueError, drafts.BadRequest) as exc:
                reason = str(exc) if isinstance(exc, drafts.BadRequest) else "unreadable request"
                drafts.write_status(self.root, request_id, "rejected", reason, now=now)
                path.unlink(missing_ok=True)
                stats.rejected += 1
                continue
            outbox = self._outbox(clean)
            if isinstance(outbox, str):
                drafts.write_status(self.root, request_id, "rejected", outbox, now=now)
                path.unlink(missing_ok=True)
                stats.rejected += 1
                continue
            if outbox[0] == "imap":
                reason = self._save_imap(outbox[1], clean)
                reply = {"ok": True} if reason is None else {"ok": False, "reason": reason}
            else:
                try:
                    reply = self.runner({"command": "draft", "request": for_mail_app(clean),
                                         "open": self.open_windows})
                except jxa.ScriptError as exc:
                    drafts.write_status(self.root, request_id, "failed", str(exc), now=now)
                    path.unlink(missing_ok=True)
                    stats.failed += 1
                    continue
            if reply.get("ok"):
                drafts.write_status(self.root, request_id, "created", now=now)
                stats.created += 1
                budget -= 1
            else:
                drafts.write_status(self.root, request_id, "failed", str(reply.get("reason", "unknown")), now=now)
                stats.failed += 1
            path.unlink(missing_ok=True)
        return stats

    def doctor(self) -> dict:
        """Counts only: no addresses, subjects or bodies."""
        report = {
            "pending": len(drafts.pending(self.root)),
            "created_last_hour": self._created_since(time.time() - 3600),
            "max_per_hour": self.max_per_hour,
        }
        if self.mail_app:
            start = time.monotonic()
            report["sender_addresses"] = len(self.runner({"command": "senders"})["addresses"])
            report["seconds_to_reach_mail"] = round(time.monotonic() - start, 2)
        if self.imap_accounts:
            import mail_imap_drafts
            report["imap_accounts"] = []
            for account_id, settings in self.imap_accounts.values():
                start = time.monotonic()
                connect = self.imap_connect or mail_imap_drafts.DraftsImap
                with connect(account_id, settings) as conn:
                    try:
                        conn.drafts_box()
                        found = True
                    except mail_imap_drafts.DraftError:
                        found = False
                report["imap_accounts"].append({
                    "account": account_id, "addresses": len(settings["addresses"]),
                    "drafts_mailbox_found": found, "seconds_to_reach_server": round(time.monotonic() - start, 2)})
        return report
