"""browser_vault action=create_login: a new login with a password the model never sees (CR 0024 R1).

With a sign-up form open, the viewer approves through the ``vault.create_login`` server request. The
answer either approves a generated password (``{"generate": true}``) or carries one the user typed
(``{"password": "..."}``); an empty answer declines. Hermes generates the password here from the form's
``minlength``, ``maxlength`` and ``passwordrules``, writes it into every new-password field of the form
over the supervisor socket (so a confirm field gets it too), checks the fields' ``pattern`` inside the
page before writing, and only then stores a ``login`` item for the page's origin. The result carries
the handle, origin and identifier, never the password.

Called with the handle it returned, it replaces that item's password and refills the form, for a site
that rejected the first one after submit.

Installed as /opt/hermes/tools/vault_create_login.py by patch-vault-create-login.py. The prompt
callback is a ContextVar, so tool workers started with ``propagate_context_to_thread`` inherit it.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import secrets
import string
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

ACTION = "create_login"
PROMPT_METHOD = "vault.create_login"
PROMPT_TIMEOUT_S = 180  # the save_login card's deadline (gateway/session_vault_prompt.py)

DEFAULT_LENGTH = 24
MAX_LENGTH = 128
DEFAULT_SYMBOLS = "-_.!@#%+=?"
# When a field's pattern refuses a candidate, retry shorter and without symbols (a pattern often
# caps the length without a maxlength attribute).
RETRY_SCHEDULE = ((True, None), (False, None), (True, 20), (False, 20), (True, 16), (False, 16),
                  (True, 12), (False, 12))

LOWER = string.ascii_lowercase
UPPER = string.ascii_uppercase
DIGIT = string.digits
# passwordrules "special" (WebKit's spec), without the space.
SPECIAL = "-~!@#$%^&*_+=`|(){}[:;\"'<>,.?]"
ASCII_PRINTABLE = "".join(chr(c) for c in range(0x21, 0x7F))

_CLASSES = {"lower": LOWER, "upper": UPPER, "digit": DIGIT, "special": SPECIAL,
            "ascii-printable": ASCII_PRINTABLE, "unicode": ASCII_PRINTABLE}

PromptCallback = Callable[..., Optional[dict]]
_prompt_callback: contextvars.ContextVar[Optional[PromptCallback]] = contextvars.ContextVar(
    "assistant_stack_vault_create_login_prompt", default=None)


def set_prompt_callback(cb: Optional[PromptCallback]) -> contextvars.Token:
    """Install this turn's create_login prompt; returns the token for reset_prompt_callback."""
    return _prompt_callback.set(cb)


def reset_prompt_callback(token: contextvars.Token) -> None:
    _prompt_callback.reset(token)


def get_prompt_callback() -> Optional[PromptCallback]:
    return _prompt_callback.get()


def parse_answer(raw: Any) -> Optional[dict]:
    """The card's answer: ``{"generate": True}``, ``{"password": str}``, or None for a decline."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text == "generate":
        return {"generate": True}
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("password"), str) and data["password"]:
        return {"password": data["password"]}
    if data.get("generate") is True:
        return {"generate": True}
    return None


# ── schema ────────────────────────────────────────────────────────────────────────────────────

SCHEMA_SENTENCE = (
    " create_login (a sign-up form is open and shows its password fields: the user approves, Hermes "
    "generates a password the site accepts, saves the login and fills every new-password field, including "
    "the confirm field; pass identifier; call again with the returned handle if the site rejects the password)."
)


def extend_schema(schema: dict) -> None:
    """Add the create_login action to BROWSER_VAULT_SCHEMA in place (before registration)."""
    props = schema.setdefault("parameters", {}).setdefault("properties", {})
    action = props.setdefault("action", {"type": "string", "enum": []})
    enum = action.setdefault("enum", [])
    if ACTION not in enum:
        enum.append(ACTION)
    props["identifier"] = {"type": "string", "description": (
        "create_login only: the email or username the user chose to register.")}
    handle = props.get("handle")
    if isinstance(handle, dict) and "create_login" not in str(handle.get("description", "")):
        handle["description"] = (str(handle.get("description") or "").rstrip()
                                 + " create_login: the handle it returned, to replace a rejected password.").strip()
    label = props.get("label")
    if isinstance(label, dict) and "create_login" not in str(label.get("description", "")):
        label["description"] = str(label.get("description") or "").replace("save_login only", "save_login / create_login")
    if SCHEMA_SENTENCE.strip() not in schema.get("description", ""):
        schema["description"] = str(schema.get("description") or "").rstrip() + SCHEMA_SENTENCE


# ── password policy ───────────────────────────────────────────────────────────────────────────

@dataclass
class PasswordRules:
    required: list[str]
    allowed: str
    max_consecutive: Optional[int] = None
    minlength: Optional[int] = None
    maxlength: Optional[int] = None


def _parse_class_list(value: str) -> str:
    chars = ""
    for custom in re.findall(r"\[([^\]]*)\]", value):
        chars += custom
    for name in re.sub(r"\[[^\]]*\]", " ", value).replace(",", " ").split():
        chars += _CLASSES.get(name.strip().lower(), "")
    return "".join(dict.fromkeys(c for c in chars if not c.isspace()))


def parse_passwordrules(text: str) -> PasswordRules:
    """Parse a ``passwordrules`` attribute (WebKit's syntax); unknown properties are ignored."""
    rules = PasswordRules(required=[], allowed="")
    for part in (text or "").split(";"):
        name, sep, value = part.partition(":")
        if not sep:
            continue
        name = name.strip().lower()
        value = value.strip()
        if name == "required":
            chars = _parse_class_list(value)
            if chars:
                rules.required.append(chars)
        elif name == "allowed":
            rules.allowed += _parse_class_list(value)
        elif name in ("max-consecutive", "minlength", "maxlength"):
            try:
                num = int(value)
            except ValueError:
                continue
            if num > 0:
                setattr(rules, name.replace("-", "_"), num)
    rules.allowed = "".join(dict.fromkeys(rules.allowed))
    return rules


@dataclass
class Policy:
    length: int
    required: list[str]
    allowed: str
    max_consecutive: Optional[int] = None


def derive_policy(fields: list[dict], *, symbols: bool = True, cap: Optional[int] = None) -> Policy:
    """The password the target fields accept, from their length limits and ``passwordrules``.
    Without hints: 24 characters with at least one lower, upper, digit and symbol. ``cap`` shortens
    it further on a retry, but never below the fields' minlength."""
    minlength = max([int(f.get("minLength") or 0) for f in fields] or [0])
    maxlengths = [int(f["maxLength"]) for f in fields if f.get("maxLength")]
    maxlength = min(maxlengths) if maxlengths else None
    rules = None
    for f in fields:
        if str(f.get("passwordrules") or "").strip():
            rules = parse_passwordrules(str(f["passwordrules"]))
            break
    max_consecutive = None
    if rules is not None:
        minlength = max(minlength, rules.minlength or 0)
        if rules.maxlength:
            maxlength = min(maxlength, rules.maxlength) if maxlength else rules.maxlength
        max_consecutive = rules.max_consecutive
    if rules is not None and (rules.required or rules.allowed):
        required = list(rules.required)
        allowed = "".join(dict.fromkeys("".join(required) + rules.allowed))
    else:
        required = [LOWER, UPPER, DIGIT] + ([DEFAULT_SYMBOLS] if symbols else [])
        allowed = "".join(required)
    if not symbols:  # a page pattern refused symbols: keep letters and digits where the rules allow
        alnum = [s for s in ("".join(c for c in r if c.isalnum()) for r in required) if s]
        if alnum and len(alnum) == len(required):
            required = alnum
            allowed = "".join(c for c in allowed if c.isalnum()) or allowed
    length = min(DEFAULT_LENGTH, cap) if cap else DEFAULT_LENGTH
    if maxlength:
        length = min(length, maxlength)
    if minlength:
        length = max(length, minlength)
    if maxlength and length > maxlength:
        length = maxlength
    return Policy(length=max(1, min(length, MAX_LENGTH)), required=required, allowed=allowed,
                  max_consecutive=max_consecutive)


def _too_consecutive(password: str, limit: Optional[int]) -> bool:
    if not limit:
        return False
    run = 1
    for prev, cur in zip(password, password[1:]):
        run = run + 1 if cur == prev else 1
        if run > limit:
            return True
    return False


def generate_password(policy: Policy) -> str:
    """One character from each required set, the rest from the allowed set, shuffled with the OS RNG."""
    rng = secrets.SystemRandom()
    for _ in range(200):
        chars = [secrets.choice(s) for s in policy.required[: policy.length]]
        chars += [secrets.choice(policy.allowed) for _ in range(policy.length - len(chars))]
        rng.shuffle(chars)
        password = "".join(chars)
        if not _too_consecutive(password, policy.max_consecutive):
            return password
    return password


# ── page scripts ──────────────────────────────────────────────────────────────────────────────

_SLOT = "data-hermes-signup-slot"

_INSPECT_JS = """(() => {
  const nonce = __NONCE__;
  const elements = [];
  const forms = [];
  (function walk(root) {
    for (const e of root.querySelectorAll("*")) {
      if (e.tagName === "INPUT") elements.push(e);
      else if (e.tagName === "FORM") forms.push(e);
      if (e.shadowRoot) walk(e.shadowRoot);
    }
  })(document);
  const out = [];
  elements.forEach((el, index) => {
    el.removeAttribute("__SLOT__");
    if (el.type !== "password" || el.disabled || el.readOnly) return;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || el.getClientRects().length === 0) return;
    el.setAttribute("__SLOT__", nonce + ":" + index);
    const labels = el.labels ? Array.from(el.labels, (l) => l.textContent || "") : [];
    const formIndex = el.form ? forms.indexOf(el.form) : -1;
    out.push({
      index,
      type: el.type,
      autocomplete: el.getAttribute("autocomplete") || "",
      formIndex: formIndex >= 0 ? formIndex : null,
      minLength: el.minLength > 0 ? el.minLength : null,
      maxLength: el.maxLength > 0 ? el.maxLength : null,
      passwordrules: el.getAttribute("passwordrules") || "",
      hasPattern: !!el.getAttribute("pattern"),
      name: [el.name, el.id].join(" "),
      label: [...labels, el.getAttribute("aria-label") || "", el.getAttribute("placeholder") || ""].join(" "),
    });
  });
  return JSON.stringify(out);
})()"""

# Writes ``value`` into every stamped target, or into none: the origin, the targets' type, their
# maxlength and their pattern are all checked before the first write. Reports only counts.
_FILL_JS = """(() => {
  const expectedOrigin = __ORIGIN__;
  if (window.location.origin !== expectedOrigin) {
    return JSON.stringify({ refused: "origin_changed", found: window.location.origin });
  }
  const nonce = __NONCE__;
  const targets = __TARGETS__;
  const value = __VALUE__;
  const roots = [];
  (function walk(root) {
    roots.push(root);
    for (const e of root.querySelectorAll("*")) if (e.shadowRoot) walk(e.shadowRoot);
  })(document);
  const find = (sel) => roots.flatMap((r) => Array.from(r.querySelectorAll(sel)));
  const cleanup = () => find("[__SLOT__]").forEach((n) => n.removeAttribute("__SLOT__"));
  const els = targets.map((i) => find('[__SLOT__="' + nonce + ':' + i + '"]')[0]);
  if (els.some((el) => !el || el.type !== "password")) { cleanup(); return JSON.stringify({ refused: "fields_changed" }); }
  if (value) {
    for (const el of els) {
      if (el.maxLength > 0 && value.length > el.maxLength) { cleanup(); return JSON.stringify({ refused: "too_long" }); }
      const p = el.getAttribute("pattern");
      if (!p) continue;
      let re = null;
      try { re = new RegExp("^(?:" + p + ")$", "v"); } catch (e) {
        try { re = new RegExp("^(?:" + p + ")$", "u"); } catch (e2) { re = null; }
      }
      if (re && !re.test(value)) { cleanup(); return JSON.stringify({ refused: "pattern" }); }
    }
  }
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
  let filled = 0;
  for (const el of els) {
    try {
      el.focus();
      if (setter && setter.set) { setter.set.call(el, value); } else { el.value = value; }
      el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: value ? "insertText" : "deleteContentBackward" }));
      el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      if (el.value === value) filled += 1;
    } catch (e) { /* skip */ }
  }
  cleanup();
  return JSON.stringify({ filled });
})()"""


def build_inspect_js(nonce: str) -> str:
    return _INSPECT_JS.replace("__SLOT__", _SLOT).replace("__NONCE__", json.dumps(nonce))


def build_fill_js(targets: list[int], value: str, expected_origin: str, nonce: str) -> str:
    return (_FILL_JS.replace("__SLOT__", _SLOT).replace("__ORIGIN__", json.dumps(expected_origin))
            .replace("__NONCE__", json.dumps(nonce)).replace("__TARGETS__", json.dumps(targets))
            .replace("__VALUE__", json.dumps(value)))


_RE_CURRENT = re.compile(r"\b(?:current|old|existing)\s*password\b")
# Sign-up wording on a password field (names are lower-cased with separators turned into spaces, and
# camelCase like repeatedPassword stays one word, so these match inside words).
_RE_NEW = re.compile(r"new|create|choose|confirm|repeat|retype|verify|again")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_\-]+", " ", str(text or "").lower())).strip()


def select_new_password_fields(controls: list[dict]) -> list[dict]:
    """The password inputs of one form that take a new password.

    Fields marked ``autocomplete="new-password"`` when the page marks any, in the form of the first
    one. Otherwise the visible password inputs not marked or labelled as the current one, in the form
    that looks most like a sign-up: two password fields (password and confirm) beat one, and sign-up
    wording (create, confirm, repeat) breaks ties, so a sign-in box in the page header is left alone.
    With nothing to tell forms apart, the first form wins."""
    fields = []
    for c in controls:
        if not isinstance(c, dict) or c.get("type") != "password":
            continue
        tokens = set(str(c.get("autocomplete") or "").lower().split())
        if tokens & {"current-password", "one-time-code"}:
            continue
        fields.append((c, tokens))
    marked = [c for c, tokens in fields if "new-password" in tokens]
    if marked:
        form = marked[0].get("formIndex")
        return [c for c in marked if c.get("formIndex") == form]
    unmarked = [c for c, _ in fields if not _RE_CURRENT.search(_norm(f"{c.get('name', '')} {c.get('label', '')}"))]
    forms: dict[Any, list[dict]] = {}
    for c in unmarked:
        forms.setdefault(c.get("formIndex"), []).append(c)

    def score(group: list[dict]) -> int:
        text = " ".join(_norm(f"{c.get('name', '')} {c.get('label', '')}") for c in group)
        return 2 * (len(group) >= 2) + bool(_RE_NEW.search(text))

    best: list[dict] = []
    for group in forms.values():  # insertion order = page order; strict > keeps the first on a tie
        if not best or score(group) > score(best):
            best = group
    return best


# ── the action ────────────────────────────────────────────────────────────────────────────────

def _err(error: str, error_type: str = "", **extra) -> str:
    out = {"success": False, "error": error, **extra}
    if error_type:
        out["error_type"] = error_type
    return json.dumps(out, ensure_ascii=False)


def _identifier_type(identifier: str) -> str:
    return "email" if "@" in identifier else ("phone" if identifier.lstrip("+").isdigit() else "username")


def _inspect(vt: Any, task_id: str) -> tuple[str, list[dict]]:
    nonce = secrets.token_hex(8)
    res = vt._eval_js(task_id, build_inspect_js(nonce))
    if not res.get("success"):
        return nonce, []
    raw = vt._parse_json_result(res.get("result"))
    if isinstance(raw, str):
        raw = vt._parse_json_result(raw)
    return nonce, select_new_password_fields(raw if isinstance(raw, list) else [])


def _write(vt: Any, task_id: str, origin: str, value: str) -> tuple[dict, list[dict]]:
    """Inspect afresh and write ``value`` into the new-password fields: (page result, targets)."""
    from agent.redact import register_vault_redaction_value

    nonce, targets = _inspect(vt, task_id)
    if not targets:
        return {"refused": "no_fields"}, []
    if value:
        register_vault_redaction_value(value)  # before the value reaches the page
    res = vt._eval_js_secret(task_id, build_fill_js([t["index"] for t in targets], value, origin, nonce))
    if not res.get("success"):
        return {"refused": "eval_failed", "error_type": res.get("error_type") or "",
                "error": str(res.get("error") or "fill failed")[:200]}, targets
    parsed = vt._parse_json_result(res.get("result"))
    if isinstance(parsed, str):
        parsed = vt._parse_json_result(parsed)
    return (parsed if isinstance(parsed, dict) else {"refused": "eval_failed"}), targets


def _replace_password(store: Any, item_id: str, password: str) -> bool:
    """Swap a local login's password in place (the store has add and remove only)."""
    with store._locked():
        items = store._read_all()
        for rec in items:
            if rec.get("id") == item_id and rec.get("kind") == "login":
                rec["secret"] = {**(rec.get("secret") or {}), "password": password}
                store._write_all(items)
                return True
    return False


_FILL_REFUSALS = {
    "origin_changed": ("origin_changed", "The page navigated before the password could be filled. Nothing was written or saved."),
    "fields_changed": ("fields_changed", "The form's password fields changed before the fill. Nothing was written or saved; call create_login again."),
    "no_fields": ("no_new_password_field", "The sign-up form's password fields are gone. Nothing was saved."),
}


def browser_vault_create_login(identifier: str = "", label: str = "", handle: str = "",
                               task_id: Optional[str] = None) -> str:
    import tools.browser_vault_tool as vt
    from agent.vault_backends.unlock import can_prompt_here
    from agent.vault_store import get_vault_store

    effective_task_id = task_id or "default"
    identifier = str(identifier or "").strip()
    store = get_vault_store()
    meta = None
    if handle:
        meta = store.get_meta(handle)
        if meta is None or meta.kind != "login":
            return _err(f"No local login with handle {handle!r}. Call create_login without a handle to make one.",
                        "unknown_handle")
        if identifier and identifier != meta.identifier:
            return _err(f"Handle {handle!r} is the login for {meta.identifier}, not {identifier}.", "identifier_mismatch")
        identifier = meta.identifier or ""
    if not identifier:
        return _err("Pass identifier: the email or username the user chose to register. Ask them if you don't know it.",
                    "identifier_required")

    vt._focus_bound_origin(effective_task_id, meta.origin if meta else "", "login")
    origin = vt._current_page_origin(effective_task_id)
    if not origin:
        return _err("Open the site's sign-up page first; the login is saved for that page's origin.")
    if meta is not None and meta.origin != origin:
        return _err(f"Handle {handle!r} belongs to {meta.origin}, but the open page is {origin}.", "origin_mismatch")

    _, fields = _inspect(vt, effective_task_id)
    if not fields:
        return _err("No new-password field on the current page. Open the sign-up form at the step that asks for a "
                    "password. For a sign-in form, use action list and fill, or save_login.", "no_new_password_field")

    prompt = get_prompt_callback()
    if prompt is None or not can_prompt_here():
        return _err(f"This session cannot ask the user to approve a new login (headless/cron/API). Tell them to create "
                    f"the account for {origin} in their own browser, then sign in with the browser-login steps.",
                    "prompt_unavailable")
    host = origin.split("://", 1)[-1]
    answer = prompt(origin, host, identifier, regenerate=meta is not None)
    if not answer:
        return _err("The user chose not to create this login. Do not ask again this turn.", "save_declined")
    typed = answer.get("password") if isinstance(answer.get("password"), str) else ""
    answer.clear()

    password = ""
    page: dict = {}
    targets: list[dict] = []
    schedule = ((True, None),) if typed else RETRY_SCHEDULE
    for symbols, cap in schedule:
        if typed:
            password = typed
        else:
            _, fields = _inspect(vt, effective_task_id)
            if not fields:
                page = {"refused": "no_fields"}
                break
            password = generate_password(derive_policy(fields, symbols=symbols, cap=cap))
        page, targets = _write(vt, effective_task_id, origin, password)
        if page.get("refused") not in ("pattern", "too_long"):
            break
    typed = ""

    refused = page.get("refused")
    if refused in ("pattern", "too_long"):
        return _err("The site's password rules rejected every generated password. Nothing was saved. Ask the user "
                    "to choose one in the card instead (call create_login again).", "password_rules_unmet")
    if refused in _FILL_REFUSALS:
        error_type, text = _FILL_REFUSALS[refused]
        return _err(text, error_type)
    if refused:
        return _err(page.get("error") or "Filling the password fields failed. Nothing was saved.",
                    page.get("error_type") or "fill_failed")
    filled = int(page.get("filled") or 0)
    if filled < len(targets):
        _write(vt, effective_task_id, origin, "")
        return _err("Not every password field took the value, so the fields were cleared and nothing was saved.",
                    "fill_failed")

    id_type = _identifier_type(identifier)
    try:
        if meta is not None:
            if not _replace_password(store, meta.id, password):
                raise RuntimeError("the login is no longer in the vault")
            item_id = meta.id
        else:
            item_id = store.add_item("login", label.strip() or host, {
                "identifier_type": id_type, "identifier": identifier, "password": password}, origin=origin).id
    except Exception as exc:  # health: allow BLE001 -- the page is cleared so no unsaved password stays in it
        _write(vt, effective_task_id, origin, "")
        from agent.vault_store import scrub_secret_from_text
        return _err("Saving the login failed, so the password fields were cleared: "
                    + scrub_secret_from_text(str(exc), {"password": password})[:200], "save_failed")
    finally:
        password = ""

    return json.dumps({
        "success": True, "handle": item_id, "origin": origin, "identifier": identifier,
        "identifier_type": id_type, "regenerated": meta is not None,
        "fill": {"success": True, "filled_fields": filled},
        "next": ("Type the identifier into the form's email or username field if it has one, then submit. Do not "
                 "type or read the password. If the site rejects the password, call browser_vault with action "
                 f"create_login and handle '{item_id}' to replace it."),
    }, ensure_ascii=False)


def handle_action(args: dict, task_id: Optional[str]) -> str:
    """browser_vault's dispatch entry for action=create_login."""
    return browser_vault_create_login(identifier=str(args.get("identifier") or ""),
                                      label=str(args.get("label") or ""),
                                      handle=str(args.get("handle") or ""), task_id=task_id)
