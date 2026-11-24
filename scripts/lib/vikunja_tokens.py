"""Mint the two Vikunja MCP tokens Hermes uses. scripts/todo.sh init runs this
through stack_python.

Reads VK_BASE, VK_USER, VK_PASS, VK_TITLE_RW and VK_TITLE_RO from the
environment, so the password and tokens stay off argv. Prints
VIKUNJA_MCP_TOKEN_RW=... and VIKUNJA_MCP_TOKEN_RO=... lines, plus LOG lines
for todo.sh to show. Tested by scripts/test-vikunja-tokens.py.
"""

import json
import os
import sys
import urllib.error
import urllib.request

base = os.environ["VK_BASE"]


def call(method, path, body=None, token=None):
    req = urllib.request.Request(base + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        sys.exit(f"{method} {path} failed: HTTP {exc.code} {detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach Vikunja at {base}: {exc.reason}")
    return json.loads(raw) if raw else None


jwt = call("POST", "/api/v1/login", {
    "username": os.environ["VK_USER"],
    "password": os.environ["VK_PASS"],
    "long_token": False,
})["token"]

# Presets from the server: "typed" is what the Settings → MCP page offers as
# the everyday read/write set (tasks, projects, labels, comments, assignees).
typed = (call("GET", "/api/v2/mcp/info", token=jwt).get("presets") or {}).get("typed") or {}
if not typed.get("tasks"):
    sys.exit("this Vikunja has no MCP presets; it needs 2.7.0 or later")
rw = {group: list(actions) for group, actions in typed.items()}
ro = {}
for group, actions in typed.items():
    reads = [a for a in actions if a in ("read_all", "read_one")]
    if reads:
        ro[group] = reads
# The preset leaves out kanban: project views and their buckets. Both tokens
# read them; the read/write one also creates, renames and deletes buckets and
# moves tasks between them. Views themselves stay read-only on both.
# Bucket permission keys are looked up by route: Vikunja 2.7.1 renamed them,
# and "views_buckets_tasks" went from the POST that moves a task (2.7.0) to
# the GET that lists bucket tasks (2.8.0).
project_routes = (call("GET", "/api/v1/routes", token=jwt) or {}).get("projects") or {}


def bucket_key(method, suffix):
    for key, route in project_routes.items():
        if route.get("method") == method and route.get("path", "").endswith(suffix):
            return key
    sys.exit(f"this Vikunja has no {method} route ending in {suffix}")


bucket_reads = [
    bucket_key("GET", "/views/:view/buckets"),
    bucket_key("GET", "/views/:view/buckets/tasks"),
]
bucket_writes = [
    bucket_key("PUT", "/views/:view/buckets"),
    bucket_key("POST", "/buckets/:bucket"),
    bucket_key("DELETE", "/buckets/:bucket"),
    bucket_key("POST", "/buckets/:bucket/tasks"),
]
for perms, extra in ((rw, bucket_reads + bucket_writes), (ro, bucket_reads)):
    projects = perms.setdefault("projects", [])
    projects.extend(a for a in extra if a not in projects)
    perms["projects_views"] = ["read_all", "read_one"]
    perms["projects_views_tasks"] = ["read_all"]
rw["mcp"] = ["access"]
ro["mcp"] = ["access"]

titles = {os.environ["VK_TITLE_RW"], os.environ["VK_TITLE_RO"]}
for tok in call("GET", "/api/v1/tokens", token=jwt) or []:
    if tok.get("title") in titles:
        call("DELETE", f"/api/v1/tokens/{tok['id']}", token=jwt)
        print(f"LOG Replaced token {tok['title']}")

# Vikunja requires an expiry. Far enough out that nothing silently breaks;
# rotate by re-running make todo-init.
expires = "2099-12-31T00:00:00Z"
for key, title, perms in (
    ("VIKUNJA_MCP_TOKEN_RW", os.environ["VK_TITLE_RW"], rw),
    ("VIKUNJA_MCP_TOKEN_RO", os.environ["VK_TITLE_RO"], ro),
):
    tok = call("PUT", "/api/v1/tokens", {"title": title, "expires_at": expires, "permissions": perms}, token=jwt)
    print(f"{key}={tok['token']}")
    print(f"LOG Created token {title} ({', '.join(sorted(perms))})")
