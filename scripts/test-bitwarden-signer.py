#!/usr/bin/env python3
"""Checks for the Bitwarden sign-in signer's pure parts (compose/bitwarden-signin/signer).

Host part: scrub.py, snapshot.py and cookies.py run without a browser. The
scrub test compares the signer's encodings with the broker's
digest_redaction.FORMS so the two copies stay equal. Values are synthetic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
from testkit import expect  # noqa: E402

SIGNER_SRC = ROOT / "compose" / "bitwarden-signin"
BROKER_SRC = ROOT / "compose" / "bitwarden-broker"
sys.path.insert(0, str(SIGNER_SRC))
sys.path.insert(0, str(BROKER_SRC))

from broker import digest_redaction  # noqa: E402
from signer import cookies, scrub, snapshot  # noqa: E402

MARKER = "«redacted-vault-secret»"
SECRET = "pa ss&wö\r\nrd/CANARY+1="


def test_scrub_matches_digest_forms() -> None:
    s = scrub.Scrubber()
    s.add(SECRET)
    stripped = SECRET.replace("\r", "").replace("\n", "")
    want = {digest_redaction._encode(v, f) for v in (SECRET, stripped) for f in digest_redaction.FORMS}
    expect(s.patterns() == want, "scrubber encodings equal the broker's digest forms")
    expect(scrub.MARKER == MARKER, "marker")


def test_scrub_replaces_each_form() -> None:
    s = scrub.Scrubber()
    s.add(SECRET)
    stripped = SECRET.replace("\r", "").replace("\n", "")
    for v in (SECRET, stripped):
        for form in digest_redaction.FORMS:
            enc = digest_redaction._encode(v, form).decode("utf-8")
            out = s.apply(f"before {enc} after")
            expect(out == f"before {MARKER} after", f"{form} of {v!r} replaced: {out!r}")
    expect(s.apply("nothing here") == "nothing here", "clean text unchanged")
    s.clear()
    expect(s.apply(SECRET) == SECRET, "clear forgets values")
    s.add("")
    expect(s.apply("abc") == "abc", "empty value ignored")


def _node(i, role, name="", *, value=None, name_from=None, ignored=False, backend=None, children=()):
    n = {"nodeId": str(i), "ignored": ignored, "role": {"value": role}, "name": {"value": name},
         "childIds": [str(c) for c in children], "backendDOMNodeId": backend if backend is not None else 100 + i}
    if value is not None:
        n["value"] = {"value": value}
    props = []
    if name_from:
        props.append({"name": "nameFrom", "value": {"value": name_from}})
    if value is not None:
        props.append({"name": "value", "value": {"value": value}})
    if props:
        n["properties"] = props
    return n


def _fixture() -> list[dict]:
    return [
        _node(1, "RootWebArea", "Sign in"),
        _node(2, "generic", "", ignored=True),
        _node(3, "textbox", "Password", value="CANARY-pw", name_from="attribute"),
        _node(4, "textbox", "CANARY-user", value="CANARY-user", name_from="contents"),
        _node(5, "searchbox", "CANARY-q", value="CANARY-q", name_from="value"),
        _node(6, "combobox", "CANARY-c", value="CANARY-c"),
        _node(7, "button", "Continue", name_from="contents"),
        _node(8, "StaticText", "Welcome back"),
    ]


def test_reduce_drops_values() -> None:
    items, refs = snapshot.reduce(_fixture())
    expect("CANARY" not in json.dumps(items, ensure_ascii=False), f"no value text in {items}")
    by_role = {i["role"]: i for i in items}
    expect(by_role["button"]["name"] == "Continue", "button name kept")
    expect(items[1]["role"] == "textbox" and items[1]["name"] == "Password", "labelled textbox name kept")
    expect(by_role["StaticText"]["text"] == "Welcome back", "static text kept as text")
    expect(set(items[0]) == {"ref", "role", "name", "text"}, "item keys")
    expect(refs[items[1]["ref"]] == 103, "ref maps to backendDOMNodeId")


def test_reduce_refs_stable_order() -> None:
    a, ra = snapshot.reduce(_fixture())
    b, rb = snapshot.reduce(_fixture())
    expect(a == b and ra == rb, "same input, same refs")
    expect([i["ref"] for i in a] == list(range(1, len(a) + 1)), "refs numbered from 1 in order")
    expect(len(a) == 7, "ignored node skipped")
    expect(ra[1] == 101 and ra[2] == 103, "ignored node takes no ref")


def _c(domain, name="s", **kw):
    base = {"name": name, "value": "v", "domain": domain, "path": "/", "expires": -1, "httpOnly": True,
            "secure": True, "sameSite": "Lax", "size": 9, "session": True}
    base.update(kw)
    return base


def test_select_parent_domain_and_excludes_sibling_and_idp() -> None:
    jar = [_c(".example.com", "parent"), _c("app.example.com", "exact"), _c("other.example.com", "sibling"),
           _c("sso.idp.test", "idp"), _c("example.com", "hostonly-parent"), _c(".idp.test", "idp-domain"),
           _c(".com", "tld")]
    out = cookies.select(jar, ("https://app.example.com",))
    names = sorted(c["name"] for c in out)
    expect(names == ["exact", "parent"], f"selected {names}")
    expect("tld" not in names, "a bare TLD domain cookie is not kept")
    expect(set(out[0]) == {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"},
           "reduced keys")
    out2 = cookies.select(jar, ("https://app.example.com", "https://sso.idp.test:8443"))
    expect(sorted(c["name"] for c in out2) == ["exact", "idp", "idp-domain", "parent"], "several origins")
    expect(cookies.select(jar, ()) == [], "no origins, no cookies")


def main() -> None:
    for fn in (test_scrub_matches_digest_forms, test_scrub_replaces_each_form, test_reduce_drops_values,
               test_reduce_refs_stable_order, test_select_parent_domain_and_excludes_sibling_and_idp):
        fn()
    print("ok")
    # Task 3 adds the image part here through hermes_image.maybe_run.


if __name__ == "__main__":
    main()
