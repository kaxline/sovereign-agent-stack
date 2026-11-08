#!/usr/bin/env python3
"""Add-on manifests: the validator's policy, and every built-in manifest
against docker-compose.yml and sync-profiles.sh.

    python3 scripts/test-addon-manifests.py
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import addon_manifest as am  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    if ok:
        print(f"ok: {label}")
    else:
        failures += 1
        print(f"FAIL: {label}{': ' + detail if detail else ''}")


def refused(label: str, manifest, source: str, needle: str) -> None:
    errors = am.validate(manifest, source)
    check(label, any(needle in e for e in errors), f"errors were {errors}")


BASE = json.loads((ROOT / "addons" / "gptr" / "addon.json").read_text())


def variant(**changes):
    m = copy.deepcopy(BASE)
    for dotted, value in changes.items():
        node = m
        keys = dotted.split("__")
        for k in keys[:-1]:
            node = node[k]
        if value is None:
            node.pop(keys[-1], None)
        else:
            node[keys[-1]] = value
    return m


def packaged(m):
    m = copy.deepcopy(m)
    m.pop("builtin", None)
    m["services"] = {"server": {"image": "ghcr.io/example/x@sha256:" + "a" * 64,
                                "port": 8000, "healthCheck": "/health"}}
    m["mcp"].pop("env", None)
    return m


# --- Policy ---------------------------------------------------------------
check("gptr manifest is valid as built-in", am.validate(BASE, "builtin") == [],
      str(am.validate(BASE, "builtin")))
check("packaged form is valid from a community feed", am.validate(packaged(BASE), "community") == [],
      str(am.validate(packaged(BASE), "community")))

refused("missing permissions", variant(permissions=None), "builtin", "'permissions'")
refused("bad id", variant(id="gptr"), "builtin", "reverse-domain")
refused("built-in without builtin block", variant(builtin=None), "builtin", "builtin block")
refused("packaged with builtin block", {**packaged(BASE), "builtin": BASE["builtin"]}, "official",
        "only built-in")
refused("packaged image by tag", variant(services={"server": {"image": "x:latest", "port": 1,
                                                              "healthCheck": "/h"}}, builtin=None),
        "community", "digest")
no_health = packaged(BASE)
del no_health["services"]["server"]["healthCheck"]
refused("packaged mcp service without healthCheck", no_health, "community", "healthCheck")
refused("pin outside tools", variant(mcp__pin=["nope"]), "builtin", "mcp.pin")
refused("writeTools outside tools", variant(mcp__writeTools=["nope"]), "builtin", "mcp.writeTools")
refused("core name collision", variant(mcp__name="mail"), "builtin", "collides")
refused("core prefix collision", variant(mcp__name="crm-main"), "builtin", "collides")
refused("bad access level", variant(mcp__profiles={"default": "admin"}), "builtin", "ro or rw")
refused("unknown stack service", variant(uses=["kafka"]), "builtin", "kafka")
refused("postgres without exposesToAgent", variant(uses=["postgres"]), "builtin", "exposesToAgent")
refused("neo4j without exposesToAgent", variant(uses=["neo4j"]), "builtin", "exposesToAgent")
refused("choice default not in options",
        variant(settings={"access": {"type": "choice", "options": ["read", "write"], "default": "ask"}}),
        "builtin", "not one of its options")
refused("mcp.env on a packaged add-on", {**packaged(BASE), "mcp": BASE["mcp"]}, "official", "mcp.env")

# The personal-data checks from the spec's "What changes".
reader = variant(permissions__reads=["messages"], permissions__cron=False,
                 mcp__profiles={"default": "ro", "browser": "ro"})
check("messages reader is valid as built-in", am.validate(reader, "builtin") == [],
      str(am.validate(reader, "builtin")))
refused("reader that sends", variant(permissions__reads=["messages"], permissions__cron=False,
                                     permissions__sends=True, mcp__profiles={"default": "ro"}),
        "builtin", "may not send")
refused("reader on api-server", variant(permissions__reads=["messages"], permissions__cron=False,
                                        mcp__profiles={"default": "ro", "api-server": "ro"}),
        "builtin", "api-server")
refused("reader with a default path",
        variant(permissions__reads=["messages"], permissions__cron=False,
                mcp__profiles={"default": "ro"},
                settings={"db": {"type": "path", "default": "~/Library/Messages/chat.db"}}),
        "builtin", "may not have a default")
refused("reader in cron", variant(permissions__reads=["mail"], permissions__cron=True,
                                  mcp__profiles={"default": "ro"}), "builtin", "cron false")
refused("reader from a community feed", packaged(reader), "community", "refused for community")
check("reader from the official feed is allowed", am.validate(packaged(reader), "official") == [],
      str(am.validate(packaged(reader), "official")))
refused("sender from a community feed", packaged(variant(permissions__sends=True)), "community",
        "sends is refused")
refused("host component from a community feed", {**packaged(BASE), "host": {"platform": "macos"}},
        "community", "host component")

# A service map gives each access level its own service.
SPLIT = json.loads((ROOT / "scripts" / "fixtures" / "addons" / "split" / "addon.json").read_text())
check("the split fixture is valid", am.validate(SPLIT, "builtin") == [], str(am.validate(SPLIT, "builtin")))


def split(**changes):
    m = copy.deepcopy(SPLIT)
    m.update(changes)
    return m


one = variant(mcp__service={"rw": "server"})
check("a map with only rw is accepted", am.validate(one, "builtin") == [], str(am.validate(one, "builtin")))
refused("a map naming an unknown service", variant(mcp__service={"rw": "nope"}), "builtin", "mcp.service")
refused("a map with a bad access key", variant(mcp__service={"rx": "server"}), "builtin", "mcp.service")
refused("a mapped service without a port",
        split(services={"rw": {"compose": "split-rw"}, "ro": {"compose": "split-ro", "port": 9000}}),
        "builtin", ".port is needed")
refused("a profile at ro when the map has no ro", variant(mcp__service={"rw": "server"},
                                                          mcp__profiles={"api-server": "ro"}),
        "builtin", "no service for ro")

# --- mcp-plan ---------------------------------------------------------------
env = {"GPTR_MCP_ENABLED": "1"}
plan = am.mcp_plan(ROOT / "addons", "browser", env)
want = ("gptr|GPT Researcher|1|rw|http://gptr-mcp:8000/sse|900|30|sse|"
        "deep_research quick_search write_report get_research_sources get_research_context|"
        "deep_research quick_search write_report|http://gptr-mcp:8000/sse")
check("plan for browser with the flag on", want in plan, str(plan))
plan = am.mcp_plan(ROOT / "addons", "api-server", env)
check("plan leaves api-server without access", any(line.startswith("gptr|GPT Researcher|1||") for line in plan),
      str(plan))
plan = am.mcp_plan(ROOT / "addons", "default",
                   {"GPTR_MCP_ENABLED": "0", "GPTR_MCP_URL": "http://elsewhere:9/sse", "GPTR_MCP_TIMEOUT": "60"})
check("plan honours the env overrides and the flag off",
      any(line.startswith("gptr|GPT Researcher|0|rw|http://elsewhere:9/sse|60|30|") for line in plan), str(plan))

with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "split").mkdir()
    (Path(tmp) / "split" / "addon.json").write_text(am.canonical(SPLIT))
    ro_url, rw_url = "http://split-ro:9000/mcp", "http://split-rw:9000/mcp"
    for prof, url in (("default", rw_url), ("browser", rw_url), ("api-server", ro_url), ("other", rw_url)):
        got = am.mcp_plan(Path(tmp), prof, {"SPLIT_MCP_ENABLED": "1"})
        check(f"split plan for {prof} picks {url[7:15]} and lists both URLs",
              len(got) == 1 and got[0].split("|")[4] == url and got[0].split("|")[10] == f"{ro_url} {rw_url}",
              str(got))

with tempfile.TemporaryDirectory() as tmp:
    bad = Path(tmp) / "broken"
    bad.mkdir()
    (bad / "addon.json").write_text("{not json")
    wrong_dir = Path(tmp) / "wrong"
    wrong_dir.mkdir()
    (wrong_dir / "addon.json").write_text(am.canonical(BASE))
    loose = Path(tmp) / "gptr"
    loose.mkdir()
    (loose / "addon.json").write_text(json.dumps(BASE))
    results = {p.parent.name: am.check_builtin_file(p, m, e) for p, m, e in am.load_dir(Path(tmp))}
    check("unparsable manifest is reported", any("cannot parse" in e for e in results["broken"]))
    check("directory must match the short id", any("short id" in e for e in results["wrong"]))
    check("non-canonical layout is refused", any("canonical" in e for e in results["gptr"]))
    check("plan skips invalid manifests", am.mcp_plan(Path(tmp), "default", env) == [])

# --- cron fence ---------------------------------------------------------------
check("an add-on with cron true is not fenced", am.cron_fence(ROOT / "addons", env) == ([], []),
      str(am.cron_fence(ROOT / "addons", env)))


def fence_of(manifests: dict[str, str], environ) -> tuple[list[str], list[str]]:
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in manifests.items():
            (Path(tmp) / name).mkdir()
            (Path(tmp) / name / "addon.json").write_text(text)
        return am.cron_fence(Path(tmp), environ)


got = fence_of({"gptr": am.canonical(variant(permissions__cron=False))}, env)
check("cron false fences the server by name and URL", got == (["gptr"], ["http://gptr-mcp:8000/sse"]), str(got))
got = fence_of({"gptr": am.canonical(variant(permissions__cron=None))},
               {**env, "GPTR_MCP_URL": "http://elsewhere:9/sse"})
check("a missing cron field fences, at the overridden URL", got == (["gptr"], ["http://elsewhere:9/sse"]),
      str(got))
got = fence_of({"split": am.canonical(SPLIT)}, {})
check("a service map fences every URL", got == (["split"], ["http://split-ro:9000/mcp", "http://split-rw:9000/mcp"]),
      str(got))
broken = variant(permissions__cron=True, permissions__reads=["mail"])
got = fence_of({"gptr": am.canonical(broken)}, env)
check("an invalid manifest is fenced by its MCP name", got[0] == ["gptr"], str(got))
got = fence_of({"gptr": json.dumps({"mcp": "nonsense"})}, env)
check("a manifest with no readable MCP name fences nothing", got == ([], []), str(got))

# --- Built-in manifests against the repo ------------------------------------
compose = (ROOT / "docker-compose.yml").read_text()
sync = ROOT / "scripts" / "sync-profiles.sh"
table = subprocess.run(["bash", str(sync), "--table"], capture_output=True, text=True, cwd=ROOT)
check("sync-profiles.sh --table runs", table.returncode == 0, table.stderr.strip())
rows = [line.split() for line in table.stdout.splitlines() if line.strip()]
flags = [r[0] for r in rows]
check("sync-profiles table has no duplicate flags", len(flags) == len(set(flags)),
      ", ".join(f for f in flags if flags.count(f) > 1))


def service_block(name: str) -> str:
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", compose, re.M | re.S)
    return m.group(1) if m else ""


for path, manifest, parse_error in am.load_dir(ROOT / "addons"):
    sid = path.parent.name
    errors = am.check_builtin_file(path, manifest, parse_error)
    check(f"{sid}: manifest is valid, canonical and in the right directory", not errors, "; ".join(errors))
    if errors:
        continue
    b = manifest["builtin"]
    seed = "1" if b["seed"] else "0"
    check(f"{sid}: sync-profiles maps {b['flag']} to {b['profile']}", [b["flag"], b["profile"], seed] in rows,
          table.stdout)
    for name, svc in manifest["services"].items():
        block = service_block(svc["compose"])
        check(f"{sid}: compose service {svc['compose']} exists", bool(block))
        profiles = re.search(r"^    profiles: \[(.*)\]$", block, re.M)
        listed = [p.strip().strip('"') for p in profiles.group(1).split(",")] if profiles else []
        check(f"{sid}: {svc['compose']} is in the {b['profile']} profile", b["profile"] in listed, str(listed))
    if manifest.get("mcp"):
        for var in [b["flag"], *manifest["mcp"].get("env", {}).values()]:
            n = len(re.findall(rf"^      - {var}=", compose, re.M))
            check(f"{sid}: both hermes-*-bootstrap services receive {var}", n == 2, f"found {n}")

for svc in ("hermes-api-bootstrap", "hermes-browser-bootstrap"):
    block = service_block(svc)
    check(f"{svc} mounts addons/", "- ./addons:/bootstrap/addons:ro" in block)
    check(f"{svc} mounts addon_manifest.py",
          "- ./scripts/lib/addon_manifest.py:/bootstrap/addon_manifest.py:ro" in block)

if failures:
    print(f"{failures} failed")
    sys.exit(1)
print("all add-on manifest checks passed")
