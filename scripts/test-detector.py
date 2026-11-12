#!/usr/bin/env python3
"""The detector add-on's service (compose/detector), against a stub provider.

Covers every check_text error code, both provider adapters, UTF-16 offsets,
and the host listener's token and Host checks. No network beyond 127.0.0.1;
nothing reaches Pangram or GPTZero.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
from testkit import Checks  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="detector-test-"))
(TMP / "config").mkdir()
(TMP / "secrets").mkdir()


# --- stub provider ----------------------------------------------------------

class Stub:
    """What the stub answers next. mode: ok, 401, 402, 429, failed, slow, nojson."""
    mode = "ok"
    normalized = None  # Pangram's returned text, when it differs from the input
    calls: list[str] = []
    polls_before_success = 1


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, status, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error_mode(self) -> bool:
        if Stub.mode == "401":
            self._json(401, {"detail": "bad key"})
        elif Stub.mode == "402":
            self._json(402, {"detail": "no credits"})
        elif Stub.mode == "429":
            self._json(429, {"detail": "slow down"}, {"Retry-After": "17"})
        elif Stub.mode == "slow":
            time.sleep(3)
            self._json(200, {})
        elif Stub.mode == "nojson":
            data = b"<html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            return False
        return True

    def do_POST(self):
        Stub.calls.append(f"POST {self.path} key={self.headers.get('x-api-key')}")
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self._error_mode():
            return
        if self.path == "/task":
            Stub.last_text = body["text"]
            Stub.polls = 0
            return self._json(200, {"task_id": "t1"})
        if self.path == "/v2/predict/text":
            text = body["document"]
            first, rest = text.split(". ", 1)
            return self._json(200, {"version": "2025-x", "documents": [{
                "completely_generated_prob": 0.83, "predicted_class": "ai",
                "sentences": [{"sentence": first + ".", "generated_prob": 0.9},
                              {"sentence": "not in the text at all", "generated_prob": 0.5},
                              {"sentence": "  " + rest.replace(" ", "\n", 1), "generated_prob": 0.2}]}]})
        self._json(404, {})

    def do_GET(self):
        Stub.calls.append(f"GET {self.path}")
        if self._error_mode():
            return
        if self.path == "/task/t1":
            Stub.polls += 1
            if Stub.mode == "failed":
                return self._json(200, {"stage": "STAGE_FAILED", "headline": "preprocessing"})
            if Stub.polls <= Stub.polls_before_success:
                return self._json(200, {"task_id": "t1", "stage": "STAGE_PROCESSING"})
            text = Stub.normalized if Stub.normalized is not None else Stub.last_text
            cut = text.index(". ") + 2
            return self._json(200, {
                "stage": "STAGE_SUCCESS", "text": text, "version": "4.0",
                "prediction_short": "Mixed", "fraction_ai": 0.25, "fraction_ai_assisted": 0.5,
                "fraction_human": 0.25,
                "windows": [
                    {"text": text[:cut], "label": "AI-Generated", "ai_assistance_score": 0.97,
                     "start_index": 0, "end_index": cut},
                    {"text": text[cut:], "label": "Human Written", "ai_assistance_score": 0.03,
                     "start_index": cut, "end_index": len(text)},
                ]})
        self._json(404, {})


stub = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
stub.daemon_threads = True
threading.Thread(target=stub.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{stub.server_address[1]}"

os.environ.update(
    DETECTOR_CONFIG_DIR=str(TMP / "config"),
    DETECTOR_SECRETS_DIR=str(TMP / "secrets"),
    DETECTOR_PANGRAM_URL=STUB,
    DETECTOR_GPTZERO_URL=STUB,
    DETECTOR_TIMEOUT="2",
    DETECTOR_POLL_INTERVAL="0.05",
)
sys.path.insert(0, str(ROOT / "compose" / "detector"))
import detector  # noqa: E402
import server  # noqa: E402

c = Checks()


def set_provider(name: str) -> None:
    (TMP / "config" / "settings.env").write_text(f"ADDON_DETECTOR_PROVIDER={name}\n")


def set_key(name: str, value: str | None) -> None:
    path = TMP / "secrets" / f"{name}_key"
    if value is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(value + "\n")


def code(result: dict) -> str | None:
    return (result.get("error") or {}).get("code")


# 60 words: the emoji before "Second" is outside the BMP, so UTF-16 offsets
# after it differ from code point offsets by one.
WORDS = " ".join(f"w{i}" for i in range(58))
TEXT = "First sentence here 😀. Second part " + WORDS

# --- offsets --------------------------------------------------------------
c.expect(detector.utf16_index("a😀b", 2) == 3, "utf16_index counts an astral character as 2")
c.expect(detector.utf16_index("abc", 3) == 3, "utf16_index matches code points for BMP text")
segs = detector.locate("one  two three", [("one two", 0.5), ("three", 2)])
c.expect(segs == [{"start": 0, "end": 8, "score": 0.5}, {"start": 9, "end": 14, "score": 1.0}],
         f"locate matches across whitespace and clamps scores: {segs}")

# --- input checks (no provider call) ------------------------------------------
set_provider("pangram")
set_key("pangram", None)
Stub.calls = []
c.expect(code(detector.check_text("too few words here")) == "too_short", "fewer than 50 words is too_short")
c.expect(code(detector.check_text("x " * 30_000)) == "too_long", "over MAX_CHARS is too_long")
c.expect(code(detector.check_text(5)) == "bad_input", "a non-string is bad_input")
c.expect(code(detector.check_text(TEXT)) == "no_key", "no key for the chosen provider is no_key")
c.expect(Stub.calls == [], f"no provider call before a key and a valid text: {Stub.calls}")
set_provider("other")
c.expect(code(detector.check_text(TEXT)) == "provider_error", "an unknown provider is provider_error")

# --- Pangram ------------------------------------------------------------------
set_provider("pangram")
set_key("pangram", "pk-test")
Stub.mode, Stub.normalized, Stub.calls = "ok", None, []
r = detector.check_text(TEXT)
c.expect(r.get("provider") == "pangram" and r.get("model") == "4.0" and r.get("label") == "Mixed",
         f"pangram result header: {r}")
c.expect(r.get("score") == 0.75, f"pangram score is fraction_ai + fraction_ai_assisted: {r.get('score')}")
cut = TEXT.index(". ") + 1  # the window's trailing space is stripped
c.expect(r.get("segments") == [
    {"start": 0, "end": cut + 1, "score": 0.97},
    {"start": cut + 2, "end": len(TEXT) + 1, "score": 0.03}],
    f"pangram segments are UTF-16 offsets into the input: {r.get('segments')}")
c.expect(isinstance(r.get("checkedAt"), str) and r["checkedAt"].endswith("Z"), "checkedAt is UTC ISO")
c.expect(Stub.calls[0] == "POST /task key=pk-test" and "GET /task/t1" in Stub.calls,
         f"pangram posts a task and polls it: {Stub.calls}")

Stub.normalized = "First   sentence here 😀. Second   part " + WORDS
r = detector.check_text(TEXT)
c.expect(len(r.get("segments", [])) == 2 and r["segments"][0]["start"] == 0
         and r["segments"][1]["end"] == len(TEXT) + 1,
         f"segments from normalized text map back to the input: {r.get('segments')}")
Stub.normalized = None

for mode, want in (("402", "quota"), ("429", "quota"), ("401", "provider_error"),
                   ("failed", "provider_error"), ("nojson", "provider_error"), ("slow", "timeout")):
    Stub.mode = mode
    r = detector.check_text(TEXT)
    c.expect(code(r) == want, f"pangram {mode} gives {want}: {r}")
    if mode == "429":
        c.expect(r["error"].get("retryAfter") == 17, f"429 carries Retry-After as retryAfter: {r}")
    if mode == "402":
        c.expect("retryAfter" not in r["error"], "402 has no retryAfter")
Stub.mode = "ok"

# --- GPTZero ------------------------------------------------------------------
set_provider("gptzero")
set_key("gptzero", "gz-test")
Stub.calls = []
r = detector.check_text(TEXT)
c.expect(r.get("provider") == "gptzero" and r.get("score") == 0.83 and r.get("label") == "ai"
         and r.get("model") == "2025-x", f"gptzero result header: {r}")
c.expect(r.get("segments") == [{"start": 0, "end": cut + 1, "score": 0.9},
                                {"start": cut + 2, "end": len(TEXT) + 1, "score": 0.2}],
         f"gptzero sentences are located, an unknown one skipped: {r.get('segments')}")
c.expect(Stub.calls == ["POST /v2/predict/text key=gz-test"], f"gptzero call: {Stub.calls}")

# --- HTTP service ---------------------------------------------------------------
set_provider("pangram")
(TMP / "secrets" / "http_token").write_text("tok-123\n")
internal = server.serve(0, require_token=False)
host = server.serve(0, require_token=True)
IPORT, HPORT = internal.server_address[1], host.server_address[1]


def call(port, path="/check", body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method=method or ("POST" if data is not None else "GET"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), dict(exc.headers)


AUTH = {"Authorization": "Bearer tok-123"}
log = io.StringIO()
with redirect_stderr(log):
    st, body, _ = call(IPORT, body={"text": TEXT})
    c.expect(st == 200 and body.get("provider") == "pangram", f"internal port needs no token: {st} {body}")
    st, body, _ = call(HPORT, body={"text": TEXT})
    c.expect(st == 401 and code(body) == "unauthorized", f"host port without a token: {st} {body}")
    st, body, _ = call(HPORT, body={"text": TEXT}, headers={"Authorization": "Bearer nope"})
    c.expect(st == 401, f"host port with a wrong token: {st}")
    st, body, _ = call(HPORT, body={"text": TEXT}, headers={**AUTH, "Host": "evil.example:8790"})
    c.expect(st == 403 and code(body) == "forbidden", f"host port with a foreign Host: {st} {body}")
    st, body, _ = call(HPORT, body={"text": TEXT}, headers=AUTH)
    c.expect(st == 200 and body.get("score") == 0.75, f"host port with the token: {st} {body}")
    st, body, _ = call(HPORT, body={"text": "short"}, headers=AUTH)
    c.expect(st == 422 and code(body) == "too_short", f"too_short over HTTP: {st} {body}")
    st, body, _ = call(HPORT, body={"txt": TEXT}, headers=AUTH)
    c.expect(st == 400 and code(body) == "bad_input", f"a body without text: {st} {body}")
    Stub.mode = "429"
    st, body, hdrs = call(HPORT, body={"text": TEXT}, headers=AUTH)
    c.expect(st == 429 and body["error"].get("retryAfter") == 17 and hdrs.get("Retry-After") == "17",
             f"quota over HTTP carries Retry-After: {st} {body}")
    Stub.mode = "ok"
    set_key("pangram", None)
    st, body, _ = call(HPORT, body={"text": TEXT}, headers=AUTH)
    c.expect(st == 409 and code(body) == "no_key", f"no_key over HTTP: {st} {body}")
    st, body, _ = call(HPORT, "/health", headers=AUTH)
    c.expect(st == 200 and body.get("ok") is True, f"health with the token: {st} {body}")
    st, body, _ = call(HPORT, "/nope", body={}, headers=AUTH)
    c.expect(st == 404, f"unknown path: {st}")
    (TMP / "secrets" / "http_token").unlink()
    st, body, _ = call(HPORT, body={"text": TEXT}, headers=AUTH)
    c.expect(st == 401, f"no token file refuses every request: {st}")
logged = log.getvalue()
c.expect("w57" not in logged and "First sentence" not in logged and "tok-123" not in logged,
         f"the log holds no text and no token: {logged!r}")
c.expect("POST /check 401 unauthorized" in logged, f"the log names method, path, status and code: {logged!r}")

c.finish("ok detector service")
