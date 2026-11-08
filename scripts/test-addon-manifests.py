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

# --- credentials and vaultBackend ---------------------------------------------
VAULT = {"name": "bwbroker", "plugin": "compose/hermes/plugins/bwbroker", "toolset": "bitwarden",
         "tools": ["bitwarden_request_access", "bitwarden_release_access"],
         "profiles": {"default": "rw", "browser": "rw"}}


def vault_manifest(**changes):
    m = variant(id="io.github.kaxline.stack.bitwarden", title="Bitwarden",
                builtin__flag="BITWARDEN_ENABLED", builtin__profile="bitwarden",
                permissions__reads=["credentials"], permissions__cron=False, mcp=None,
                vaultBackend=copy.deepcopy(VAULT))
    m["services"] = {"broker": {"compose": "bwbroker", "port": 8099}}
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


check("vault manifest is valid as built-in", am.validate(vault_manifest(), "builtin") == [],
      str(am.validate(vault_manifest(), "builtin")))
refused("credentials_cron_refused", vault_manifest(permissions__cron=True), "builtin",
        "needs permissions.cron false")
refused("credentials_api_server_refused",
        vault_manifest(vaultBackend__profiles={"default": "rw", "api-server": "rw"}), "builtin",
        "api-server")
for reserved in ("vault", "op", "bw"):
    refused(f"vault_backend_reserved_name {reserved}", vault_manifest(vaultBackend__name=reserved),
            "builtin", "vaultBackend.name is reserved")
refused("vault_backend_bad_name", vault_manifest(vaultBackend__name="Bad-Name"), "builtin",
        "vaultBackend.name must")
packaged_vault = vault_manifest(builtin=None)
packaged_vault["services"] = {"broker": {"image": "ghcr.io/example/x@sha256:" + "a" * 64,
                                         "port": 8099, "healthCheck": "/health"}}
refused("vault_backend_community_refused", packaged_vault, "community",
        "vaultBackend is refused unless the add-on is built-in or on the official feed")
check("vaultBackend from the official feed is allowed",
      am.validate(packaged_vault, "official") == [], str(am.validate(packaged_vault, "official")))
refused("vault_backend_shape: missing plugin", vault_manifest(vaultBackend__plugin=None), "builtin",
        "vaultBackend.plugin")
refused("vault_backend_shape: plugin escapes", vault_manifest(vaultBackend__plugin="a/../x"),
        "builtin", "vaultBackend.plugin")
refused("vault_backend_shape: absolute plugin", vault_manifest(vaultBackend__plugin="/x/y"),
        "builtin", "vaultBackend.plugin")
refused("vault_backend_shape: missing toolset", vault_manifest(vaultBackend__toolset=None), "builtin",
        "vaultBackend.toolset")
refused("vault_backend_shape: bad toolset", vault_manifest(vaultBackend__toolset="Bit warden"), "builtin",
        "vaultBackend.toolset")
refused("vault_backend_shape: tools not a list", vault_manifest(vaultBackend__tools="a b"),
        "builtin", "vaultBackend.tools")
refused("vault_backend_shape: bad profile value",
        vault_manifest(vaultBackend__profiles={"default": "admin"}), "builtin", "ro or rw")
refused("vaultBackend is not an object", vault_manifest(vaultBackend="x"), "builtin",
        "vaultBackend must be an object")
refused("vaultBackend without profiles", vault_manifest(vaultBackend__profiles={}), "builtin",
        "vaultBackend.profiles")

# --- vault-plan ---------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp) / "bitwarden"
    d.mkdir()
    (d / "addon.json").write_text(am.canonical(vault_manifest()))
    tail = "compose/hermes/plugins/bwbroker|bitwarden_request_access bitwarden_release_access"
    plan = am.vault_plan(Path(tmp), "default", {"BITWARDEN_ENABLED": "1"})
    check("vault_plan_lines", plan == [f"bwbroker|1|rw|{tail}"], str(plan))
    plan = am.vault_plan(Path(tmp), "api-server", {"BITWARDEN_ENABLED": "1"})
    check("vault plan leaves api-server without access", plan == [f"bwbroker|1||{tail}"], str(plan))
    plan = am.vault_plan(Path(tmp), "default", {})
    check("vault plan flag off", plan == [f"bwbroker|0|rw|{tail}"], str(plan))
    check("mcp-plan ignores a vault-only add-on", am.mcp_plan(Path(tmp), "default", {}) == [])
    (d / "addon.json").write_text(am.canonical(vault_manifest(vaultBackend__plugin="x|y")))
    check("vault plan skips a value with a pipe", am.vault_plan(Path(tmp), "default", {}) == [])
    (d / "addon.json").write_text("{broken")
    check("vault plan skips invalid manifests", am.vault_plan(Path(tmp), "default", {}) == [])
check("vault plan is empty for the repo's add-ons today", am.vault_plan(ROOT / "addons", "default", {}) == [])

# --- mcp-plan ---------------------------------------------------------------
env = {"GPTR_MCP_ENABLED": "1"}
plan = am.mcp_plan(ROOT / "addons", "browser", env)
want = ("gptr|GPT Researcher|1|rw|http://gptr-mcp:8000/sse|900|30|sse|"
        "deep_research quick_search write_report get_research_sources get_research_context|"
        "deep_research quick_search write_report")
check("plan for browser with the flag on", want in plan, str(plan))
plan = am.mcp_plan(ROOT / "addons", "api-server", env)
check("plan leaves api-server without access", any(line.startswith("gptr|GPT Researcher|1||") for line in plan),
      str(plan))
plan = am.mcp_plan(ROOT / "addons", "default",
                   {"GPTR_MCP_ENABLED": "0", "GPTR_MCP_URL": "http://elsewhere:9/sse", "GPTR_MCP_TIMEOUT": "60"})
check("plan honours the env overrides and the flag off",
      any(line.startswith("gptr|GPT Researcher|0|rw|http://elsewhere:9/sse|60|30|") for line in plan), str(plan))

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
broken = variant(permissions__cron=True, permissions__reads=["mail"])
got = fence_of({"gptr": am.canonical(broken)}, env)
check("an invalid manifest is fenced by its MCP name", got[0] == ["gptr"], str(got))
got = fence_of({"gptr": json.dumps({"mcp": "nonsense"})}, env)
check("a manifest with no readable MCP name fences nothing", got == ([], []), str(got))

check("no plugin toolset is fenced for the repo's add-ons today", am.cron_fence_toolsets(ROOT / "addons") == [])


def toolsets_of(manifests: dict[str, str]) -> list[tuple[str, str]]:
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in manifests.items():
            (Path(tmp) / name).mkdir()
            (Path(tmp) / name / "addon.json").write_text(text)
        return am.cron_fence_toolsets(Path(tmp))


got = toolsets_of({"bitwarden": am.canonical(vault_manifest())})
check("a vaultBackend with cron false fences its toolset", got == [("bwbroker", "bitwarden")], str(got))
got = toolsets_of({"bitwarden": am.canonical(vault_manifest(permissions__reads=[], permissions__cron=True))})
check("a vaultBackend with cron true is not fenced", got == [], str(got))
got = toolsets_of({"bitwarden": am.canonical(vault_manifest(permissions__cron=True))})
check("an invalid vault manifest is fenced by its toolset", got == [("bwbroker", "bitwarden")], str(got))
got = toolsets_of({"gptr": am.canonical(variant(permissions__cron=False))})
check("an MCP-only add-on has no plugin toolset to fence", got == [], str(got))

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
