"""One Chromium per sign-in session, driven over the DevTools protocol.

``Chromium.start`` runs the Hermes image's Chromium headless with a fresh
profile directory under /tmp, in a new process group, with its sandbox on.
There is no way to start it with ``--no-sandbox``: a host where the sandbox
cannot start gets ``StartError`` and the server reports
``sandbox_unavailable``. CDP listens on loopback; the port and browser path come
from the profile's ``DevToolsActivePort`` file, so nothing is fetched over HTTP.

``call`` sends one command and reads replies until its own comes back; events
are dropped. A command still waiting at ``deadline`` (set per action by the
session) raises ``Timeout``. A protocol error raises ``CdpError`` carrying only
the method name, never the error text, which can quote page content or an
argument. A lost connection raises ``BrowserGone``.

``stop`` kills the process group it created and deletes the profile.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

CHROME_GLOB = "/opt/hermes/tools/chromium-*/chrome-linux/chrome"
PROFILE_PREFIX = "bitwarden-signin-"
START_SECONDS = 20.0
MAX_MESSAGE = 64 * 1024 * 1024


class StartError(Exception):
    """Chromium did not start (no sandbox, no binary, or no CDP port)."""


class CdpError(Exception):
    """A CDP command returned an error. Holds the method name only."""


class Timeout(Exception):
    """The action's deadline passed while a command was waiting."""


class BrowserGone(Exception):
    """The CDP connection closed: Chromium exited or was killed."""


def _chrome_path() -> str:
    found = sorted(glob.glob(CHROME_GLOB))
    if not found:
        raise StartError()
    return found[-1]


class Chromium:
    def __init__(self, proc: subprocess.Popen, profile: str, ws) -> None:
        self.proc = proc
        self.profile = profile
        self._ws = ws
        self._next_id = 0
        self.deadline: float | None = None
        self.page_session: str | None = None

    @classmethod
    def start(cls, sandbox: bool = True) -> "Chromium":
        if not sandbox:
            raise ValueError("the signer never runs Chromium without its sandbox")
        chrome = _chrome_path()
        profile = tempfile.mkdtemp(prefix=PROFILE_PREFIX, dir="/tmp")
        args = [chrome, "--headless=new", "--disable-dev-shm-usage", "--no-first-run",
                "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"]
        try:
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            shutil.rmtree(profile, ignore_errors=True)
            raise StartError() from None
        browser = cls(proc, profile, None)
        try:
            port, path = browser._wait_for_port()
            browser._ws = connect(f"ws://127.0.0.1:{port}{path}", proxy=None, compression=None,
                                  max_size=MAX_MESSAGE, open_timeout=START_SECONDS, ping_interval=None)
            browser._attach()
        except BaseException:
            browser.stop()
            raise
        return browser

    def _wait_for_port(self) -> tuple[int, str]:
        portfile = os.path.join(self.profile, "DevToolsActivePort")
        end = time.monotonic() + START_SECONDS
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise StartError()
            try:
                with open(portfile, encoding="ascii") as fh:
                    lines = fh.read().split()
            except (OSError, UnicodeDecodeError):
                lines = []
            if len(lines) >= 2 and lines[0].isdigit() and lines[1].startswith("/devtools/browser/"):
                return int(lines[0]), lines[1]
            time.sleep(0.05)
        raise StartError()

    def _attach(self) -> None:
        self.deadline = time.monotonic() + START_SECONDS
        try:
            targets = self.call("Target.getTargets")["targetInfos"]
            target = next((t["targetId"] for t in targets if t.get("type") == "page"), None)
            if target is None:
                raise StartError()
            self.page_session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
            for domain in ("Page", "DOM", "Runtime", "Accessibility"):
                self.call(f"{domain}.enable", session=self.page_session)
        except (CdpError, Timeout, BrowserGone, KeyError, TypeError):
            raise StartError() from None
        finally:
            self.deadline = None

    def call(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        if self._ws is None:
            raise BrowserGone()
        self._next_id += 1
        msg_id = self._next_id
        msg = {"id": msg_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        try:
            self._ws.send(json.dumps(msg))
            while True:
                wait = None
                if self.deadline is not None:
                    wait = self.deadline - time.monotonic()
                    if wait <= 0:
                        raise Timeout()
                try:
                    raw = self._ws.recv(timeout=wait)
                except TimeoutError:
                    raise Timeout() from None
                reply = json.loads(raw)
                if reply.get("id") != msg_id:
                    continue
                if "error" in reply:
                    raise CdpError(method)
                return reply.get("result") or {}
        except (ConnectionClosed, OSError):
            raise BrowserGone() from None

    def page(self, method: str, params: dict | None = None) -> dict:
        """``call`` on the attached page target."""
        return self.call(method, params, self.page_session)

    def stop(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - closing a dead connection; the process is killed next
                pass
        try:
            # start_new_session made Chromium the leader of its own group, so this
            # reaches its zygote and renderers and nothing else. The leader is not
            # reaped before this, so the group id still names that group.
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        shutil.rmtree(self.profile, ignore_errors=True)
