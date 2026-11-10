#!/usr/bin/env python3
"""Checks that browser_exec names a hostname that doesn't resolve instead of
calling it a private address (patch-browser-unresolvable-host.py).

Host part is stdlib-only. With --image (or when Docker and the pinned Hermes
image are present and --host-only is not given), the overlay is applied inside
nousresearch/hermes-agent and DNS answers are faked, so no network is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402

IMAGE_DRIVER = r'''
import socket
import subprocess
import sys

out = subprocess.run([sys.executable, "/bootstrap/patch-browser-unresolvable-host.py"], capture_output=True, text=True)
assert "patched" in out.stdout and "not patched" not in out.stdout, out.stdout + out.stderr
again = subprocess.run([sys.executable, "/bootstrap/patch-browser-unresolvable-host.py"], capture_output=True, text=True)
assert "already applied" in again.stdout, again.stdout + again.stderr

sys.path.insert(0, "/opt/hermes")
from tools import browser_session_cloud, url_safety
from tools import browser_url_policy as policy

# A remote browser (CDP to the host), as in this stack: private URLs are checked.
browser_session_cloud.is_local_backend = lambda: False
browser_session_cloud.allow_private_urls = lambda: False

ANSWERS = {
    "public.example": [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))],
    "intranet.example": [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443))],
}


def fake_getaddrinfo(hostname, port=None):
    if hostname in ANSWERS:
        return ANSWERS[hostname]
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


url_safety._getaddrinfo = fake_getaddrinfo

missing = policy._url_policy_error("https://csc.utah.example/Search")
assert missing and missing["success"] is False, missing
assert "csc.utah.example" in missing["error"] and "doesn't exist" in missing["error"], missing
assert "private or internal" not in missing["error"], missing

# Real private targets keep upstream's block and text.
for url in ("https://intranet.example/", "http://10.0.0.5/admin"):
    err = policy._url_policy_error(url)
    assert err and err["error"] == "Blocked: URL targets a private or internal address", (url, err)

# Public hosts still pass.
assert policy._url_policy_error("https://public.example/page") is None

print("image ok")
'''


def main() -> None:
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
