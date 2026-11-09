"""browser_vault action=capture_secret: store a value a page shows without the model seeing it (CR 0024 R2).

Run on the page that shows a new API key (or similar token). Without a ``selector`` Hermes scans the
page itself: inputs and text areas (read-only too), ``code``/``pre``/``kbd``/``samp`` and other leaf
elements whose text is one long high-entropy token. One candidate is used directly; several come back
as numbered locations with the last four characters, and the model picks one with ``candidate``. The
scan reads values over the supervisor socket, so they reach Hermes and never a tool result.

The viewer approves through the ``vault.capture_secret`` server request, which names the site, the
``name``, the destination and the last four characters. Then the value goes to one of:

- ``env``: the profile ``.env`` under ``name`` (``save_env_value_secure``, as the ``secret`` card
  does), for keys a skill or provider uses. Names that steer Hermes itself (``HERMES_*``, base URLs,
  ``PATH`` and the like) are refused, and the card says when an existing value would be replaced.
- ``viewer``: an encrypted file beside the vault (``captured.json.enc``, the vault's key), under a
  ``capture_id`` the request and the result both carry. The client takes it with the ``vault.take``
  RPC, which returns the value once and deletes it; an untaken capture expires after an hour. The
  value never travels in a server→client frame, which every attached viewer receives and a
  reconnecting viewer is replayed.

The value is registered for model-facing redaction as soon as it is read, so a later ``browser_exec``
that prints the page shows ``«redacted-vault-secret»`` instead.

Installed as /opt/hermes/tools/vault_capture_secret.py by patch-vault-capture-secret.py.
"""
from __future__ import annotations

import contextvars
import json
import math
import re
import secrets
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Callable, Optional

ACTION = "capture_secret"
PROMPT_METHOD = "vault.capture_secret"
PROMPT_TIMEOUT_S = 180
TAKE_METHOD = "vault.take"
DESTINATIONS = ("env", "viewer")
CAPTURE_TTL_S = 3600
MAX_VALUE_LEN = 4096
MIN_SECRET_LEN = 20

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
# Names whose value would change where Hermes connects, what it runs or how it authenticates itself.
RESERVED_PREFIXES = ("HERMES_", "API_SERVER_", "GATEWAY_", "TERMINAL_", "BROWSER_", "LD_", "DYLD_", "PYTHON",
                     "UV_", "SSL_", "REQUESTS_", "CURL_", "NODE_", "DOCKER_", "XDG_")
RESERVED_SUFFIXES = ("_BASE_URL", "_URL", "_HOST", "_ENDPOINT", "_PROXY", "_PORT", "_PATH", "_HOME", "_DIR")
RESERVED_NAMES = frozenset({"PATH", "HOME", "SHELL", "USER", "LANG", "TZ", "PWD", "HTTP_PROXY", "HTTPS_PROXY",
                            "NO_PROXY", "ALL_PROXY"})

PromptCallback = Callable[..., bool]
_prompt_callback: contextvars.ContextVar[Optional[PromptCallback]] = contextvars.ContextVar(
    "assistant_stack_vault_capture_prompt", default=None)


def set_prompt_callback(cb: Optional[PromptCallback]) -> contextvars.Token:
    return _prompt_callback.set(cb)


def reset_prompt_callback(token: contextvars.Token) -> None:
    _prompt_callback.reset(token)


def get_prompt_callback() -> Optional[PromptCallback]:
    return _prompt_callback.get()


def parse_answer(raw: Any) -> bool:
    """The card's answer: ``"approve"`` or ``{"approve": true}`` approves; anything else declines."""
    if not isinstance(raw, str) or not raw.strip():
        return False
    text = raw.strip()
    if text == "approve":
        return True
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("approve") is True


def name_problem(name: str) -> str:
    """Why ``name`` cannot be used, or ""."""
    if not NAME_RE.match(name or ""):
        return "name must be an environment-variable name: capitals, digits and underscores, 3 to 64 characters, e.g. PREM_API_KEY."
    if name in RESERVED_NAMES or name.startswith(RESERVED_PREFIXES) or name.endswith(RESERVED_SUFFIXES):
        return f"{name} is reserved: it would change how Hermes or a tool connects or runs. Pick a name ending in _API_KEY, _TOKEN or _SECRET."
    return ""


# ── schema ────────────────────────────────────────────────────────────────────────────────────

SCHEMA_SENTENCE = (
    " capture_secret (the page shows a new API key or token: Hermes reads it, the user approves, and it is "
    "stored without you seeing it; pass name, destination env for a key a skill uses or viewer for one the app "
    "keeps; optional selector or candidate when several values are found; never read or print the page while "
    "the key is on screen)."
)


def extend_schema(schema: dict) -> None:
    props = schema.setdefault("parameters", {}).setdefault("properties", {})
    enum = props.setdefault("action", {"type": "string", "enum": []}).setdefault("enum", [])
    if ACTION not in enum:
        enum.append(ACTION)
    props["name"] = {"type": "string", "description": (
        "capture_secret only: the variable name to store the value under, e.g. PREM_API_KEY.")}
    props["destination"] = {"type": "string", "enum": list(DESTINATIONS), "description": (
        "capture_secret only: env = the profile .env (a skill or provider reads it); viewer = handed to the "
        "user's app, which stores it itself.")}
    props["selector"] = {"type": "string", "description": (
        "capture_secret only: CSS selector of the element holding the value, when you know it.")}
    props["candidate"] = {"type": "integer", "description": (
        "capture_secret only: the number of a candidate a previous capture_secret listed.")}
    if SCHEMA_SENTENCE.strip() not in schema.get("description", ""):
        schema["description"] = str(schema.get("description") or "").rstrip() + SCHEMA_SENTENCE


# ── finding the value ─────────────────────────────────────────────────────────────────────────

_SCAN_JS = """(() => {
  const selector = __SELECTOR__;
  const roots = [];
  (function walk(root) {
    roots.push(root);
    for (const e of root.querySelectorAll("*")) if (e.shadowRoot) walk(e.shadowRoot);
  })(document);
  const visible = (el) => {
    const s = getComputedStyle(el);
    return s.display !== "none" && s.visibility !== "hidden" && el.getClientRects().length > 0;
  };
  const describe = (el) => {
    const labels = el.labels ? Array.from(el.labels, (l) => l.textContent || "") : [];
    const text = [el.getAttribute("aria-label") || "", ...labels, el.getAttribute("name") || "",
                  el.id ? "#" + el.id : "", el.getAttribute("data-testid") || ""].join(" ").replace(/\\s+/g, " ").trim();
    return (el.tagName.toLowerCase() + (text ? " " + text : "")).slice(0, 80);
  };
  const valueOf = (el) => (el.tagName === "INPUT" || el.tagName === "TEXTAREA") ? el.value : el.textContent;
  const out = [];
  if (selector) {
    for (const r of roots) {
      let el = null;
      try { el = r.querySelector(selector); } catch (e) { return JSON.stringify({ error: "bad_selector" }); }
      if (el) {
        /* A field the user or a vault fill types into, as in the page scan below: never copied out. */
        if (el.tagName === "INPUT" && el.type === "password" && !el.readOnly) return JSON.stringify({ error: "password_field" });
        out.push({ where: describe(el), value: String(valueOf(el) || "").trim() });
        break;
      }
    }
    return JSON.stringify({ candidates: out });
  }
  const tags = new Set(["CODE", "PRE", "KBD", "SAMP", "SPAN", "DIV", "P", "TD", "DD", "STRONG", "B", "MARK"]);
  for (const r of roots) {
    for (const el of r.querySelectorAll("*")) {
      if (!visible(el)) continue;
      if (el.tagName === "INPUT") {
        if (["hidden", "submit", "button", "checkbox", "radio", "file", "image", "reset"].includes(el.type)) continue;
        if (el.type === "password" && !el.readOnly) continue;  /* a field the user types into, not a shown key */
      } else if (el.tagName !== "TEXTAREA") {
        if (!tags.has(el.tagName) || el.children.length > 0) continue;
      }
      const value = String(valueOf(el) || "").trim();
      if (value.length >= __MIN__ && value.length <= __MAX__ && !/\\s/.test(value)) out.push({ where: describe(el), value });
    }
  }
  return JSON.stringify({ candidates: out });
})()"""


def build_scan_js(selector: str = "") -> str:
    return (_SCAN_JS.replace("__SELECTOR__", json.dumps(selector or "")).replace("__MIN__", str(MIN_SECRET_LEN))
            .replace("__MAX__", str(MAX_VALUE_LEN)))


# Truthy on a tab showing a key-like value: picks the dashboard tab among the ones browser_exec opened.
TAB_PROBE = ("(() => { try { return JSON.parse(" + build_scan_js().replace("\n", " ")
             + ").candidates.some((c) => /[0-9]/.test(c.value) && /[A-Za-z]/.test(c.value)); } catch (e) { return false; } })()")


_KEY_PREFIX = re.compile(r"^(?:sk|pk|rk|ak|key|api|tok|ghp|gho|ghs|github_pat|glpat|xox[abpr]|AKIA|hf|r8|pplx|gsk|"
                         r"nvapi|fw|tgp|prem)[-_]", re.IGNORECASE)
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9_\-.:+/=~]+$")


def entropy(value: str) -> float:
    counts = Counter(value)
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def looks_like_secret(value: str) -> bool:
    """Long, one token, key-like characters, and either a known key prefix or letters and digits with
    high entropy. URLs, paths, e-mail addresses, plain words and bare numbers are not."""
    if not (MIN_SECRET_LEN <= len(value) <= MAX_VALUE_LEN) or not _TOKEN_CHARS.match(value):
        return False
    if "://" in value or value.startswith(("/", "www.")) or "@" in value:
        return False
    if _KEY_PREFIX.match(value):
        return True
    # A random key this long almost always has a digit; page words and slugs ("Billing-Keys") rarely do.
    if not re.search(r"[0-9]", value) or not re.search(r"[A-Za-z]", value):
        return False
    return entropy(value) >= (3.0 if len(value) >= 32 else 3.5)


def pick_candidates(raw: list[dict]) -> list[dict]:
    """Key-like values, first location of each distinct value, in page order."""
    seen: set[str] = set()
    out = []
    for c in raw:
        value = str(c.get("value") or "") if isinstance(c, dict) else ""
        if value in seen or not looks_like_secret(value):
            continue
        seen.add(value)
        out.append({"where": str(c.get("where") or "")[:80], "value": value})
    return out


def masked(value: str) -> str:
    return "…" + value[-4:]


# ── captured values for the viewer ────────────────────────────────────────────────────────────

def _capture_store():
    from agent.vault_store import VaultStore

    class CaptureStore(VaultStore):
        """The vault's key, lock and atomic writes, over its own file: captures never show in
        vault.list or browser_vault list, and nothing that fills pages can reach them."""

        def __init__(self):
            super().__init__()
            self._vault_path = self._base / "captured.json.enc"

    return CaptureStore()


def _live(items: list[dict], now: float) -> list[dict]:
    return [rec for rec in items if float(rec.get("expires_at") or 0) > now]


def put_capture(capture_id: str, name: str, origin: str, value: str) -> None:
    store = _capture_store()
    now = time.time()
    with store._locked():
        items = _live(store._read_all(), now)
        items.append({"id": capture_id, "name": name, "origin": origin, "value": value,
                      "created_at": datetime.now(UTC).isoformat(), "expires_at": now + CAPTURE_TTL_S})
        store._write_all(items)


def take_capture(capture_id: str) -> Optional[dict]:
    """The capture's ``{name, origin, value}``, removed from the file; None when unknown or expired."""
    store = _capture_store()
    if not store._vault_path.exists():
        return None
    now = time.time()
    with store._locked():
        items = store._read_all()
        live = _live(items, now)
        found = next((rec for rec in live if rec.get("id") == capture_id), None)
        remaining = [rec for rec in live if rec.get("id") != capture_id]
        if len(remaining) != len(items):
            store._write_all(remaining)
    if found is None:
        return None
    return {"name": found.get("name", ""), "origin": found.get("origin", ""), "value": found.get("value", "")}


# ── the action ────────────────────────────────────────────────────────────────────────────────

def _err(error: str, error_type: str = "", **extra) -> str:
    out = {"success": False, "error": error, **extra}
    if error_type:
        out["error_type"] = error_type
    return json.dumps(out, ensure_ascii=False)


def _scan(vt: Any, task_id: str, selector: str) -> tuple[Optional[str], list[dict]]:
    """(error_type, candidates) read over the supervisor socket; values never pass through argv."""
    res = vt._eval_js_secret(task_id, build_scan_js(selector))
    if not res.get("success"):
        return res.get("error_type") or "scan_failed", []
    parsed = vt._parse_json_result(res.get("result"))
    if isinstance(parsed, str):
        parsed = vt._parse_json_result(parsed)
    if not isinstance(parsed, dict):
        return "scan_failed", []
    if parsed.get("error"):
        return str(parsed["error"]), []
    raw = parsed.get("candidates") if isinstance(parsed.get("candidates"), list) else []
    if selector:
        return None, [{"where": str(c.get("where") or "")[:80], "value": str(c.get("value") or "")}
                      for c in raw[:1] if isinstance(c, dict) and str(c.get("value") or "")]
    return None, pick_candidates(raw)


def browser_vault_capture_secret(name: str = "", destination: str = "", selector: str = "",
                                 candidate: Any = None, task_id: Optional[str] = None) -> str:
    import tools.browser_vault_tool as vt
    from agent.redact import register_vault_redaction_value
    from agent.vault_backends.unlock import can_prompt_here

    effective_task_id = task_id or "default"
    name = str(name or "").strip()
    destination = str(destination or "").strip().lower()
    if destination not in DESTINATIONS:
        return _err("Pass destination: env (the profile .env, for a key a skill or provider uses) or viewer "
                    "(handed to the user's app).", "destination_required")
    problem = name_problem(name)
    if problem:
        return _err(problem, "bad_name")

    vt._TAB_PROBES.setdefault("secret", TAB_PROBE)
    vt._focus_bound_origin(effective_task_id, "", "secret")
    origin = vt._current_page_origin(effective_task_id)
    if not origin:
        return _err("Open the page that shows the value first.")
    error_type, found = _scan(vt, effective_task_id, str(selector or "").strip())
    for c in found:
        register_vault_redaction_value(c["value"])  # before anything can echo it
    if error_type == "bad_selector":
        return _err("That selector is not valid CSS.", "bad_selector")
    if error_type == "password_field":
        return _err("That selector is an editable password field, which holds a typed or filled password, not a "
                    "key the site shows. capture_secret does not read it.", "password_field")
    if error_type:
        return _err("Could not read the page.", error_type)
    if not found:
        return _err("No key-like value is visible on this page. If the site hides it behind a reveal or show "
                    "button, click that first; if it shows only a copy button, tell the user where the key is "
                    "so they can paste it into the app instead. Do not print the page text to look for it.",
                    "no_secret_found")
    if candidate not in (None, ""):
        try:
            index = int(candidate)
        except (TypeError, ValueError):
            index = 0
        if not 1 <= index <= len(found):
            return _err(f"candidate must be between 1 and {len(found)}.", "bad_candidate")
        chosen = found[index - 1]
    elif len(found) == 1:
        chosen = found[0]
    else:
        return _err("Several key-like values are on the page. Call capture_secret again with candidate set to the "
                    "number of the right one, using where it is and its last four characters.",
                    "several_candidates",
                    candidates=[{"candidate": i + 1, "where": c["where"], "last4": masked(c["value"]),
                                 "length": len(c["value"])} for i, c in enumerate(found)])
    value = chosen["value"]
    for c in found:
        c["value"] = ""

    replaces = False
    if destination == "env":
        try:
            from hermes_cli.config import load_env
            replaces = bool(load_env().get(name))
        except Exception:  # health: allow BLE001 -- an unreadable .env only loses the replace warning
            replaces = False

    prompt = get_prompt_callback()
    if prompt is None or not can_prompt_here():
        return _err("This session cannot ask the user to approve storing the value (headless/cron/API). Tell them "
                    "where the key is on the page so they can copy it into the app themselves.", "prompt_unavailable")
    host = origin.split("://", 1)[-1]
    capture_id = f"capture_{secrets.token_hex(16)}" if destination == "viewer" else ""
    approved = prompt(origin=origin, site=host, name=name, destination=destination, last4=masked(value),
                      replaces=replaces, capture_id=capture_id)
    if not approved:
        return _err("The user chose not to store this value. Do not ask again this turn.", "save_declined")

    try:
        if destination == "env":
            from hermes_cli.config import save_env_value_secure
            save_env_value_secure(name, value)
        else:
            put_capture(capture_id, name, origin, value)
    except Exception as exc:  # health: allow BLE001 -- reported without the value
        from agent.vault_store import scrub_secret_from_text
        return _err("Storing the value failed: " + scrub_secret_from_text(str(exc), {"value": value})[:200],
                    "save_failed")
    finally:
        value_len, last4 = len(value), masked(value)
        value = ""

    out = {"success": True, "name": name, "destination": destination, "origin": origin, "last4": last4,
           "length": value_len, "replaced": replaces}
    if destination == "viewer":
        out["capture_id"] = capture_id
        out["next"] = ("The value was handed to the user's app. Close the dialog or move on; do not read or print "
                       "the page while the key is on screen.")
    else:
        out["next"] = (f"Stored in the profile .env as {name}. Close the dialog or move on; do not read or print "
                       "the page while the key is on screen.")
    return json.dumps(out, ensure_ascii=False)


def handle_action(args: dict, task_id: Optional[str]) -> str:
    """browser_vault's dispatch entry for action=capture_secret."""
    return browser_vault_capture_secret(name=str(args.get("name") or ""),
                                        destination=str(args.get("destination") or ""),
                                        selector=str(args.get("selector") or ""),
                                        candidate=args.get("candidate"), task_id=task_id)
