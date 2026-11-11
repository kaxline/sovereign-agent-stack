"""check_text: score text with a commercial AI-text detector. See docs/detector.md.

Standard library only, so tests run it on the host. The provider and its key
are read on every call, from the add-on's settings.env and secret files, so
`addon.sh configure detector` takes effect without a restart.

Every result is the CR 0030 shape:

    {provider, model?, score, label?, segments: [{start, end, score}], checkedAt}

or {error: {code, message[, retryAfter]}}. `start` and `end` are UTF-16 code
unit offsets into the submitted text (JavaScript string indices), end
exclusive. Nothing here logs or returns the submitted text.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROVIDERS = ("pangram", "gptzero")
# Checked here, before any provider call, so a too-short or too-long text costs
# nothing. Pangram bills per started 1,000 words.
MIN_WORDS = 50
MAX_CHARS = 50_000

CONFIG_DIR = Path(os.environ.get("DETECTOR_CONFIG_DIR", "/run/addon/config"))
SECRETS_DIR = Path(os.environ.get("DETECTOR_SECRETS_DIR", "/run/addon/secrets"))
PANGRAM_URL = os.environ.get("DETECTOR_PANGRAM_URL", "https://text.external-api.pangram.com")
PANGRAM_MODEL = os.environ.get("DETECTOR_PANGRAM_MODEL", "pangram-4")
GPTZERO_URL = os.environ.get("DETECTOR_GPTZERO_URL", "https://api.gptzero.me")
TIMEOUT = float(os.environ.get("DETECTOR_TIMEOUT", "60"))
POLL_INTERVAL = float(os.environ.get("DETECTOR_POLL_INTERVAL", "1"))


class CheckError(Exception):
    def __init__(self, code: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after

    def body(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.retry_after:
            err["retryAfter"] = self.retry_after
        return {"error": err}


# --- settings ----------------------------------------------------------------

def _settings() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = (CONFIG_DIR / "settings.env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if len(parts) == 1 and "=" in parts[0]:
            k, v = parts[0].split("=", 1)
            out[k] = v
    return out


def provider() -> str:
    return _settings().get("ADDON_DETECTOR_PROVIDER", "pangram")


def _key(name: str) -> str:
    try:
        return (SECRETS_DIR / f"{name}_key").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# --- offsets -----------------------------------------------------------------

def utf16_index(text: str, i: int) -> int:
    """UTF-16 code units in text[:i]; i counts code points."""
    return len(text[:i].encode("utf-16-le")) // 2


def locate(text: str, pieces: list[tuple[str, float]]) -> list[dict]:
    """Find each (piece, score) in `text` in order and return UTF-16 segments.

    A provider may return its own copy of the text (Pangram normalizes it) or
    sentences without offsets (GPTZero), so pieces are matched against the
    submitted text: exactly first, then with any run of whitespace matching
    any other. A piece that can't be found is left out."""
    out = []
    cursor = 0
    for piece, score in pieces:
        words = piece.split()
        if not words:
            continue
        at = text.find(piece.strip(), cursor)
        if at >= 0:
            start, end = at, at + len(piece.strip())
        else:
            m = re.compile(r"\s+".join(re.escape(w) for w in words)).search(text, cursor)
            if not m:
                continue
            start, end = m.start(), m.end()
        out.append({"start": utf16_index(text, start), "end": utf16_index(text, end),
                    "score": _unit(score)})
        cursor = end
    return out


def _unit(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, v))


# --- HTTP ----------------------------------------------------------------------

def _request(method: str, url: str, headers: dict, body: dict | None, timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=max(timeout, 1)) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from None
    except (socket.timeout, TimeoutError):
        raise CheckError("timeout", "the detector did not answer in time") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise CheckError("timeout", "the detector did not answer in time") from None
        raise CheckError("provider_error", "could not reach the detector") from None
    try:
        out = json.loads(raw)
    except ValueError:
        raise CheckError("provider_error", "the detector returned something that isn't JSON") from None
    if not isinstance(out, dict):
        raise CheckError("provider_error", "the detector returned an unexpected answer")
    return out


def _http_error(exc: urllib.error.HTTPError) -> CheckError:
    status = exc.code
    if status in (402, 429):
        retry = None
        after = exc.headers.get("Retry-After") if exc.headers else None
        if after and after.strip().isdigit() and int(after) > 0:
            retry = int(after)
        why = "out of credits" if status == 402 else "over its rate limit"
        return CheckError("quota", f"the detector account is {why}", retry)
    if status in (401, 403):
        return CheckError("provider_error", "the detector refused the API key")
    if status == 413:
        return CheckError("too_long", "the detector refused the text as too long")
    return CheckError("provider_error", f"the detector answered HTTP {status}")


def _deadline_left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise CheckError("timeout", "the detector did not finish in time")
    return left


# --- providers -----------------------------------------------------------------

def _pangram(text: str, key: str, deadline: float) -> dict:
    """POST /task, then poll GET /task/{id} (docs.pangram.com, async API)."""
    headers = {"x-api-key": key}
    task = _request("POST", f"{PANGRAM_URL}/task", headers,
                    {"text": text, "model": PANGRAM_MODEL}, _deadline_left(deadline))
    task_id = task.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise CheckError("provider_error", "the detector returned no task id")
    while True:
        res = _request("GET", f"{PANGRAM_URL}/task/{task_id}", headers, None, _deadline_left(deadline))
        stage = res.get("stage")
        if stage == "STAGE_SUCCESS":
            break
        if stage == "STAGE_FAILED":
            raise CheckError("provider_error", "the detector could not process the text")
        time.sleep(min(POLL_INTERVAL, _deadline_left(deadline)))
    windows = res.get("windows") if isinstance(res.get("windows"), list) else []
    pieces = []
    returned = res.get("text") if isinstance(res.get("text"), str) else None
    for w in windows:
        if not isinstance(w, dict):
            continue
        piece = w.get("text")
        if not isinstance(piece, str) and returned is not None:
            s, e = w.get("start_index"), w.get("end_index")
            if isinstance(s, int) and isinstance(e, int):
                piece = returned[s:e]
        if isinstance(piece, str):
            pieces.append((piece, w.get("ai_assistance_score")))
    out = {
        "provider": "pangram",
        "score": _unit(_unit(res.get("fraction_ai")) + _unit(res.get("fraction_ai_assisted"))),
        "segments": locate(text, pieces),
    }
    if isinstance(res.get("version"), str):
        out["model"] = res["version"]
    if isinstance(res.get("prediction_short"), str):
        out["label"] = res["prediction_short"]
    return out


def _gptzero(text: str, key: str, deadline: float) -> dict:
    """POST /v2/predict/text. Field names follow third-party descriptions of the
    API; GPTZero's own reference was not readable when this was written."""
    res = _request("POST", f"{GPTZERO_URL}/v2/predict/text", {"x-api-key": key},
                   {"document": text}, _deadline_left(deadline))
    docs = res.get("documents")
    doc = docs[0] if isinstance(docs, list) and docs and isinstance(docs[0], dict) else None
    if doc is None:
        raise CheckError("provider_error", "the detector returned no result")
    sentences = doc.get("sentences") if isinstance(doc.get("sentences"), list) else []
    pieces = [(s["sentence"], s.get("generated_prob")) for s in sentences
              if isinstance(s, dict) and isinstance(s.get("sentence"), str)]
    out = {
        "provider": "gptzero",
        "score": _unit(doc.get("completely_generated_prob")),
        "segments": locate(text, pieces),
    }
    if isinstance(res.get("version"), str):
        out["model"] = res["version"]
    if isinstance(doc.get("predicted_class"), str):
        out["label"] = doc["predicted_class"]
    return out


ADAPTERS = {"pangram": _pangram, "gptzero": _gptzero}


# --- check_text ----------------------------------------------------------------

def check_text(text) -> dict:
    """The CR 0030 result for `text`, or {error}. Never raises."""
    try:
        return _check(text)
    except CheckError as exc:
        return exc.body()
    except Exception:  # noqa: BLE001 - one bad answer must not take the service down
        return CheckError("provider_error", "the detector's answer could not be read").body()


def _check(text) -> dict:
    if not isinstance(text, str):
        raise CheckError("bad_input", "text must be a string")
    if len(text) > MAX_CHARS:
        raise CheckError("too_long", f"text is over {MAX_CHARS} characters")
    if len(text.split()) < MIN_WORDS:
        raise CheckError("too_short", f"text needs at least {MIN_WORDS} words")
    name = provider()
    if name not in ADAPTERS:
        raise CheckError("provider_error", f"unknown provider {name!r}")
    key = _key(name)
    if not key:
        raise CheckError("no_key", f"no API key is set for {name}")
    out = ADAPTERS[name](text, key, time.monotonic() + TIMEOUT)
    out["checkedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return out
