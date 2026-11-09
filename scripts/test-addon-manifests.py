#!/usr/bin/env python3
"""Add-on manifests: the validator's policy, and every built-in manifest
against docker-compose.yml and sync-profiles.sh.

    python3 scripts/test-addon-manifests.py
"""

from __future__ import annotations

import copy
import json
import re
import shutil
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
    for entry in m["mcp"] if isinstance(m["mcp"], list) else [m["mcp"]]:
        entry.pop("env", None)
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
repo_plan = am.vault_plan(ROOT / "addons", "default", {"BITWARDEN_ENABLED": "1"})
check("vault plan for the repo's add-ons is the Bitwarden broker on default",
      repo_plan == [f"bwbroker|1|rw|{tail}"], str(repo_plan))
repo_plan = am.vault_plan(ROOT / "addons", "api-server", {})
check("the repo's Bitwarden add-on has no api-server access and is off without its flag",
      repo_plan == [f"bwbroker|0||{tail}"], str(repo_plan))

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

check("env.access is accepted", am.validate(SPLIT, "builtin") == [], str(am.validate(SPLIT, "builtin")))
refused("an unknown env key", split(mcp={**SPLIT["mcp"], "env": {"acess": "SPLIT_ACCESS"}}),
        "builtin", "mcp.env")
refused("env.access must be an env var name",
        split(mcp={**SPLIT["mcp"], "env": {"access": "lower"}}), "builtin", "mcp.env")

ACCESS_URL = {"read": "http://split-ro:9000/mcp", "write": "http://split-rw:9000/mcp",
              "": "http://split-rw:9000/mcp", "Read": None, "ask": None, "readonly": None}
with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "split").mkdir()
    (Path(tmp) / "split" / "addon.json").write_text(am.canonical(SPLIT))
    for cap, want_url in ACCESS_URL.items():
        for prof in ("default", "api-server"):
            line = am.mcp_plan(Path(tmp), prof, {"SPLIT_MCP_ENABLED": "1", "SPLIT_ACCESS": cap})[0].split("|")
            if want_url is None:
                ok = line[3] == ""
            elif prof == "api-server":
                ok = line[3] == "ro"
            else:
                ok = line[3] == ("ro" if cap == "read" else "rw") and line[4] == want_url
            check(f"SPLIT_ACCESS={cap!r} on {prof}: access {line[3]!r}", ok, str(line))
    line = am.mcp_plan(Path(tmp), "default", {"SPLIT_MCP_ENABLED": "1"})[0].split("|")
    check("SPLIT_ACCESS unset keeps rw", line[3] == "rw", str(line))

# permissions.cron: true, false or "ro".
check("cron ro with a service map that has ro is accepted", SPLIT["permissions"]["cron"] == "ro"
      and am.validate(SPLIT, "builtin") == [], str(am.validate(SPLIT, "builtin")))
refused("cron ro on an add-on that reads", split(permissions={**SPLIT["permissions"], "reads": ["mail"]}),
        "builtin", "permissions.cron false")
refused("cron ro with a string service", variant(permissions__cron="ro"), "builtin", "cron ro needs an ro service")
refused("cron ro with a map that has no ro service",
        split(mcp={**SPLIT["mcp"], "service": {"rw": "rw"}, "profiles": {"default": "rw"}}),
        "builtin", "cron ro needs an ro service")
refused("cron yes", variant(permissions__cron="yes"), "builtin", "permissions.cron")

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
with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "gptr").mkdir()
    shutil.copy(ROOT / "addons" / "gptr" / "addon.json", Path(tmp) / "gptr" / "addon.json")
    check("an add-on with cron true is not fenced", am.cron_fence(Path(tmp), env) == ([], []),
          str(am.cron_fence(Path(tmp), env)))


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
SPLIT_CRON_FALSE = split(permissions={**SPLIT["permissions"], "cron": False})
got = fence_of({"split": am.canonical(SPLIT_CRON_FALSE)}, {})
check("a service map with cron false fences every URL",
      got == (["split"], ["http://split-ro:9000/mcp", "http://split-rw:9000/mcp"]), str(got))
got = fence_of({"split": am.canonical(SPLIT)}, {})
check("cron ro fences only the rw URL, and no name", got == ([], ["http://split-rw:9000/mcp"]), str(got))
broken = variant(permissions__cron=True, permissions__reads=["mail"])
got = fence_of({"gptr": am.canonical(broken)}, env)
check("an invalid manifest is fenced by its MCP name", got[0] == ["gptr"], str(got))
got = fence_of({"gptr": json.dumps({"mcp": "nonsense"})}, env)
check("a manifest with no readable MCP name fences nothing", got == ([], []), str(got))

check("the repo's Bitwarden toolset is fenced from cron",
      am.cron_fence_toolsets(ROOT / "addons") == [("bwbroker", "bitwarden")],
      str(am.cron_fence_toolsets(ROOT / "addons")))


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

# --- gates and several MCP entries ---------------------------------------------
def two_entry(**changes):
    m = variant(**changes)
    m["gates"] = [{"flag": "GPTR_EXTRA_ENABLED", "profile": "research-extra", "seed": False}]
    first = copy.deepcopy(m["mcp"])
    second = {**copy.deepcopy(m["mcp"]), "name": "gptr-extra", "title": "Extra",
              "when": {"gate": "GPTR_EXTRA_ENABLED"}}
    second.pop("env", None)
    second["service"] = "extra"
    m["services"]["extra"] = {"compose": "gptr-extra", "port": 8001}
    m["mcp"] = [first, second]
    return m


check("a two-entry manifest is valid", am.validate(two_entry(), "builtin") == [],
      str(am.validate(two_entry(), "builtin")))
two_bad_gate = two_entry()
two_bad_gate["mcp"][1]["when"] = {"gate": "NOPE_ENABLED"}
refused("when names an unknown gate", two_bad_gate, "builtin", "when.gate")
refused("gates on a packaged add-on", packaged(two_entry()), "official", "gates")
two_dup = two_entry()
two_dup["mcp"][1]["name"] = "gptr"
refused("two entries share a name", two_dup, "builtin", "mcp names must be unique")
two_shared = two_entry()
two_shared["mcp"][1]["service"] = "server"
refused("two entries share a service", two_shared, "builtin", "mcp entries 0 and 1 share service 'server'")
two_shared["mcp"][1]["service"] = {"ro": "extra", "rw": "server"}
refused("a service map overlapping another entry's service", two_shared, "builtin",
        "mcp entries 0 and 1 share service 'server'")
two_api = two_entry(permissions__reads=["messages"], permissions__cron=False)
for entry in two_api["mcp"]:
    entry["profiles"] = {"default": "ro"}
two_api["mcp"][1]["profiles"] = {"default": "ro", "api-server": "ro"}
refused("api-server on any entry of a reads add-on", two_api, "builtin", "api-server")
two_gate_shape = two_entry()
two_gate_shape["gates"] = [{"flag": "lower", "profile": "x", "seed": "no"}]
refused("a gate needs an env var name", two_gate_shape, "builtin", "gates[0].flag")
refused("a gate needs a boolean seed", two_gate_shape, "builtin", "gates[0].seed")
# sync-profiles.sh reads gates line by line with sed, so key order is part of the format.
two_order = two_entry()
two_order["gates"] = [{"seed": False, "flag": "GPTR_EXTRA_ENABLED", "profile": "research-extra"}]
refused("gate keys must be flag, profile, seed in that order", two_order, "builtin", "gates keys")
two_empty = two_entry()
two_empty["mcp"] = []
refused("an empty mcp list", two_empty, "builtin", "mcp")
two_svc = two_entry()
two_svc["services"]["server"]["when"] = {"gate": "NOPE_ENABLED"}
refused("a service when names an unknown gate", two_svc, "builtin", "services.server.when.gate")
two_svc["services"]["server"]["when"] = {"gate": "GPTR_EXTRA_ENABLED"}
check("a service may carry a known when", am.validate(two_svc, "builtin") == [],
      str(am.validate(two_svc, "builtin")))
two_ro = two_entry(permissions__cron="ro")
refused("cron ro is checked on every entry", two_ro, "builtin", "cron ro needs an ro service")

with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "gptr").mkdir()
    (Path(tmp) / "gptr" / "addon.json").write_text(am.canonical(two_entry()))
    plan = am.mcp_plan(Path(tmp), "default", {"GPTR_MCP_ENABLED": "0", "GPTR_EXTRA_ENABLED": "1"})
    first, second = (p.split("|") for p in plan) if len(plan) == 2 else ([], [])
    check("mcp-plan prints one line per entry", len(plan) == 2, str(plan))
    check("the first entry follows builtin.flag", first[:3] == ["gptr", "GPT Researcher", "0"], str(plan))
    check("the second entry follows its gate and title", second[:3] == ["gptr-extra", "Extra", "1"], str(plan))
    check("each line keeps the 11 fields", len(first) == 11 and len(second) == 11, str(plan))
    plan = am.mcp_plan(Path(tmp), "default", {"GPTR_MCP_ENABLED": "1"})
    check("a gate that is unset is off", [p.split("|")[2] for p in plan] == ["1", "0"], str(plan))

for prof in ("default", "api-server", "browser"):
    fixture = (ROOT / "scripts" / "fixtures" / f"addon-mcp-plan-{prof}.txt").read_text().splitlines()
    check(f"mcp-plan over addons/ for {prof} matches the fixture",
          am.mcp_plan(ROOT / "addons", prof, {}) == fixture, str(am.mcp_plan(ROOT / "addons", prof, {})))

got = fence_of({"gptr": am.canonical(two_entry(permissions__cron=False))}, env)
check("cron false fences every entry", got[0] == ["gptr", "gptr-extra"], str(got))

# --- settings, instances, commands and mounts -------------------------------
def with_settings(settings, **changes):
    return variant(settings=settings, **changes)


rich = variant(
    builtin__flag="GPTR_MCP_ENABLED",
    gates=[{"flag": "GPTR_EXTRA_ENABLED", "profile": "research-extra", "seed": False}],
    settings={
        "count": {"type": "int", "default": 3, "label": "How many"},
        "tags": {"type": "list", "default": ["a", "b"]},
        "since": {"type": "date", "optional": True},
        "extra": {"type": "bool", "default": False, "flag": "GPTR_EXTRA_ENABLED"},
        "token": {"type": "secret"},
    },
    instances={"name": "account", "settings": {
        "host": {"type": "string"},
        "port": {"type": "port", "default": 993},
        "password": {"type": "secret"},
    }},
    commands={"sync": {"service": "server", "instance": True, "argv": ["sync", "--all"]}},
)
rich["services"]["worker"] = {"compose": "gptr-worker", "mounts": {"secrets": "ro", "config": "ro",
                                                                    "instances": "rw"}}
check("a manifest using every new field is valid", am.validate(rich, "builtin") == [],
      str(am.validate(rich, "builtin")))

refused("list default must be strings", with_settings({"t": {"type": "list", "default": [1]}}),
        "builtin", "settings.t.default")
refused("date default must be YYYY-MM-DD", with_settings({"d": {"type": "date", "default": "2026-13-40"}}),
        "builtin", "settings.d.default")
refused("int default must be an int", with_settings({"n": {"type": "int", "default": "3"}}),
        "builtin", "settings.n.default")
refused("optional must be a bool", with_settings({"n": {"type": "int", "optional": "yes"}}),
        "builtin", "settings.n.optional")
refused("flag only on bool", with_settings({"n": {"type": "int", "default": 1, "flag": "GPTR_MCP_ENABLED"}}),
        "builtin", "settings.n.flag")
refused("flag must be a known flag",
        with_settings({"b": {"type": "bool", "default": True, "flag": "NOPE_ENABLED"}}),
        "builtin", "settings.b.flag")
refused("a setting flag may not be builtin.flag",
        with_settings({"b": {"type": "bool", "default": True, "flag": "GPTR_MCP_ENABLED"}}),
        "builtin", "settings.b.flag must name a flag listed in gates")
refused("a secret setting may not have a default", with_settings({"t": {"type": "secret", "default": "x"}}),
        "builtin", "settings.t: a secret setting may not have a default")
refused("an instance secret may not have a default",
        variant(instances={"name": "acct", "settings": {"p": {"type": "secret", "default": ""}}}),
        "builtin", "instances.settings.p: a secret setting may not have a default")
refused("setting names that differ only by case collide",
        with_settings({"api": {"type": "string"}, "API": {"type": "string"}}),
        "builtin", "settings.API: same settings.env key as 'api'")
refused("setting names that differ only by - and _ collide",
        with_settings({"api-key": {"type": "string"}, "api_key": {"type": "string"}}),
        "builtin", "settings.api_key: same settings.env key as 'api-key'")
refused("instance setting names that collide",
        variant(instances={"name": "acct", "settings": {"Host": {"type": "string"},
                                                        "host": {"type": "string"}}}),
        "builtin", "instances.settings.host: same settings.env key as 'Host'")
refused("a setting may not be called slug", with_settings({"slug": {"type": "string"}}),
        "builtin", "reserved")
refused("flag inside an instance",
        variant(instances={"name": "acct", "settings": {
            "b": {"type": "bool", "default": True, "flag": "GPTR_MCP_ENABLED"}}}),
        "builtin", "refused inside an instance")
refused("instance schema may not declare slug",
        variant(instances={"name": "acct", "settings": {"slug": {"type": "string"}}}),
        "builtin", "reserved")
refused("instances need a name", variant(instances={"settings": {"a": {"type": "string"}}}),
        "builtin", "instances.name")
refused("secret settings from a community feed",
        packaged(variant(settings={"token": {"type": "secret"}})), "community", "secret settings")
refused("secret instance settings from a community feed",
        packaged(variant(instances={"name": "acct", "settings": {"p": {"type": "secret"}}})),
        "community", "secret settings")
check("secret settings are allowed from the official feed",
      am.validate(packaged(variant(settings={"token": {"type": "secret"}})), "official") == [],
      str(am.validate(packaged(variant(settings={"token": {"type": "secret"}})), "official")))

refused("unknown mount key", variant(services__server__mounts={"logs": "ro"}), "builtin", "mounts")
refused("mount mode as a list", variant(services__server__mounts={"config": ["ro"]}), "builtin", "mounts")
refused("mount mode as a dict", variant(services__server__mounts={"config": {}}), "builtin", "mounts")
refused("command service as a list",
        variant(commands={"x": {"service": ["server"], "instance": False, "argv": ["a"]}}),
        "builtin", "commands.x.service")
refused("command service as a dict",
        variant(commands={"x": {"service": {}, "instance": False, "argv": ["a"]}}),
        "builtin", "commands.x.service")
refused("setting type as a list", with_settings({"t": {"type": ["int"]}}), "builtin", "settings.t.type")
refused("setting type as a dict", with_settings({"t": {"type": {}}}), "builtin", "settings.t.type")
refused("bad mount mode", variant(services__server__mounts={"secrets": "wo"}), "builtin", "mounts")
refused("an mcp service may not mount secrets", variant(services__server__mounts={"secrets": "ro"}),
        "builtin", "holds secrets")
refused("a command needs a known service",
        variant(commands={"x": {"service": "nope", "instance": False, "argv": ["a"]}}),
        "builtin", "commands.x.service")
refused("a command needs argv",
        variant(commands={"x": {"service": "server", "instance": False, "argv": []}}),
        "builtin", "commands.x.argv")
refused("a command needs instance as a bool",
        variant(commands={"x": {"service": "server", "instance": "yes", "argv": ["a"]}}),
        "builtin", "commands.x.instance")
refused("a reader's command may not run in an MCP service",
        variant(permissions__reads=["messages"], permissions__cron=False,
                mcp__profiles={"default": "ro"},
                commands={"x": {"service": "server", "instance": False, "argv": ["a"]}}),
        "builtin", "command runs in an MCP service")
reader_cmd = copy.deepcopy(rich)
reader_cmd.update(permissions={**reader_cmd["permissions"], "reads": ["messages"], "cron": False})
reader_cmd["mcp"]["profiles"] = {"default": "ro"}
reader_cmd["settings"].pop("token")
reader_cmd["commands"]["sync"]["service"] = "worker"
check("a reader's command may run in a non-MCP service", am.validate(reader_cmd, "builtin") == [],
      str(am.validate(reader_cmd, "builtin")))

volumed = variant(services__server__volumes=["gptr_outputs"])
check("services.<name>.volumes lists compose volume names", am.validate(volumed, "builtin") == [],
      str(am.validate(volumed, "builtin")))
refused("volumes must be a list", variant(services__server__volumes="gptr_outputs"), "builtin",
        "services.server.volumes")
refused("volumes may not be empty", variant(services__server__volumes=[]), "builtin",
        "services.server.volumes")
refused("volume names are compose names", variant(services__server__volumes=["../etc"]), "builtin",
        "services.server.volumes")
refused("volume names are strings", variant(services__server__volumes=[1]), "builtin",
        "services.server.volumes")
refused("volumes listed twice", variant(services__server__volumes=["a", "a"]), "builtin",
        "services.server.volumes")
packaged_vol = packaged(BASE)
packaged_vol["services"]["server"]["volumes"] = ["gptr_outputs"]
refused("only built-in add-ons name compose volumes", packaged_vol, "official", "services.server.volumes")

# --- Built-in manifests against the repo ------------------------------------
compose = (ROOT / "docker-compose.yml").read_text()
sync = ROOT / "scripts" / "sync-profiles.sh"
table = subprocess.run(["bash", str(sync), "--table"], capture_output=True, text=True, cwd=ROOT)
check("sync-profiles.sh --table runs", table.returncode == 0, table.stderr.strip())
rows = [line.split() for line in table.stdout.splitlines() if line.strip()]
flags = [r[0] for r in rows]
check("sync-profiles table has no duplicate flags", len(flags) == len(set(flags)),
      ", ".join(f for f in flags if flags.count(f) > 1))


# --- Lifecycle fields: oneshot, hooks, instance hooks, onToggle, data, backup ---
LIFE_REPO = Path(tempfile.mkdtemp(prefix="addon-life-"))
(LIFE_REPO / "scripts").mkdir()
(LIFE_REPO / "scripts" / "crm.sh").write_text("#!/bin/sh\n")
(LIFE_REPO / "docker-compose.yml").write_text(
    "services:\n  gptr-mcp:\n    volumes:\n"
    "      - ${FIXTURE_DATA_DIR:-./data/fixture}:/data\n"
    "      - ./data/plain:/plain:ro\n"
    "      - ./data/other:/other\n"
    "volumes:\n  cache_a:\n")


def life(**changes):
    m = variant(**changes)
    m.setdefault("commands", {})
    m["commands"].setdefault("seed-init", {"service": "server", "instance": False, "argv": ["init"]})
    m["commands"].setdefault("sync", {"service": "server", "instance": True, "argv": ["sync"]})
    return m


def life_errors(manifest, source="builtin"):
    return am.validate(manifest, source, repo=LIFE_REPO)


def life_refused(label, manifest, needle, source="builtin"):
    errors = life_errors(manifest, source)
    check(label, any(needle in e for e in errors), f"errors were {errors}")


EVERYTHING = life(
    services__job={"compose": "gptr-job", "oneshot": True},
    hooks={"init": {"command": "seed-init"}, "upgrade": {"script": ["scripts/crm.sh", "upgrade"]},
           "backup": {"script": ["scripts/crm.sh", "backup"]},
           "restore": {"script": ["scripts/crm.sh", "restore"]}},
    instances={"name": "workspace", "settings": {"url": {"type": "url"}},
               "connect": {"script": ["scripts/crm.sh", "connect"]},
               "disconnect": {"script": ["scripts/crm.sh", "disconnect"]},
               "errors": ["bad_access", "file-access-not-allowed"]},
    onToggle={"recreate": ["hermes"], "script": ["scripts/crm.sh", "follow"]},
    data={"paths": [{"env": "FIXTURE_DATA_DIR", "default": "./data/fixture"},
                    {"default": "./data/plain"}],
          "volumes": ["cache_a"]},
    backup={"secrets": ["CRM_TOKEN"], "recommended": ["CRM_URL"]})
check("a manifest with every lifecycle field is valid", life_errors(EVERYTHING) == [],
      str(life_errors(EVERYTHING)))
check("data_volumes reads data.volumes", am.data_volumes(EVERYTHING) == ["cache_a"],
      str(am.data_volumes(EVERYTHING)))
check("hook() finds init, connect and a missing one",
      am.hook(EVERYTHING, "init") == {"command": "seed-init"}
      and am.hook(EVERYTHING, "connect") == {"script": ["scripts/crm.sh", "connect"]}
      and am.hook(variant(), "init") is None and am.hook(EVERYTHING, "nope") is None)

old_form = life(services__server__volumes=["cache_a", "cache_b"])
check("services.<k>.volumes alone is still valid", life_errors(old_form) == [], str(life_errors(old_form)))
check("data_volumes reads the old services.<k>.volumes field",
      am.data_volumes(old_form) == ["cache_a", "cache_b"], str(am.data_volumes(old_form)))
life_refused("data.volumes together with services.<k>.volumes fails",
             life(services__server__volumes=["cache_a"], data={"volumes": ["cache_a"]}),
             "data.volumes")

life_refused("oneshot on the mcp target fails", life(services__server__oneshot=True), "oneshot")
life_refused("oneshot with when fails",
             life(gates=[{"flag": "GPTR_X_ENABLED", "profile": "x", "seed": False},],
                  services__job={"compose": "j", "oneshot": True, "when": {"gate": "GPTR_X_ENABLED"}}),
             "oneshot")
life_refused("oneshot must be true or false", life(services__job={"compose": "j", "oneshot": "yes"}),
             "oneshot")
packaged_one = packaged(BASE)
packaged_one["services"]["job"] = {"image": "ghcr.io/example/x@sha256:" + "a" * 64, "oneshot": True}
life_refused("oneshot is built-in only", packaged_one, "oneshot", "official")

life_refused("a hook with both command and script fails",
             life(hooks={"init": {"command": "seed-init", "script": ["scripts/crm.sh"]}}), "hooks.init")
life_refused("a hook with neither key fails", life(hooks={"init": {}}), "hooks.init")
life_refused("a script outside scripts/ fails",
             life(hooks={"init": {"script": ["bin/crm.sh"]}}), "hooks.init")
life_refused("a script with a path component fails",
             life(hooks={"init": {"script": ["scripts/../crm.sh"]}}), "hooks.init")
life_refused("a script that does not exist fails",
             life(hooks={"init": {"script": ["scripts/missing.sh"]}}), "hooks.init")
life_refused("an argv item with a line break fails",
             life(hooks={"init": {"script": ["scripts/crm.sh", "a\nb"]}}), "hooks.init")
life_refused("an empty argv item fails",
             life(hooks={"init": {"script": ["scripts/crm.sh", ""]}}), "hooks.init")
life_refused("a hook as a bare string fails",
             life(hooks={"backup": "scripts/crm.sh backup"}), "hooks.backup")
life_refused("a command hook naming an instance command fails",
             life(hooks={"init": {"command": "sync"}}), "hooks.init")
life_refused("a command hook naming no command fails",
             life(hooks={"init": {"command": "nope"}}), "hooks.init")
life_refused("an unknown hook name fails", life(hooks={"explode": {"command": "seed-init"}}), "hooks")
life_refused("a script hook is built-in only",
             packaged(life(hooks={"init": {"script": ["scripts/crm.sh"]}})), "hooks.init", "official")
check("a command hook is allowed from a community feed",
      life_errors(packaged(life(hooks={"init": {"command": "seed-init"}})), "community") == [],
      str(life_errors(packaged(life(hooks={"init": {"command": "seed-init"}})), "community")))

CONNECT = {"script": ["scripts/crm.sh", "connect"]}
life_refused("connect without disconnect fails",
             life(instances={"name": "w", "settings": {"url": {"type": "url"}}, "connect": CONNECT}),
             "connect and instances.disconnect")
life_refused("disconnect without connect fails",
             life(instances={"name": "w", "settings": {"url": {"type": "url"}}, "disconnect": CONNECT}),
             "connect and instances.disconnect")
for bad in ("timeout", "not_found", "Upper", "1x", ""):
    life_refused(f"instances.errors entry {bad!r} fails",
                 life(instances={"name": "w", "settings": {"url": {"type": "url"}}, "errors": [bad]}),
                 "instances.errors")
life_refused("instances.errors entries must be distinct",
             life(instances={"name": "w", "settings": {"url": {"type": "url"}}, "errors": ["a", "a"]}),
             "instances.errors")
life_refused("instances.errors must be a list",
             life(instances={"name": "w", "settings": {"url": {"type": "url"}}, "errors": "a"}),
             "instances.errors")

for bad in ("../x", "/etc", "./data/../x", "./data", "./data/", "./data/unused"):
    life_refused(f"data.paths default {bad!r} fails", life(data={"paths": [{"default": bad}]}),
                 "data.paths")
life_refused("data.paths env must be an env name",
             life(data={"paths": [{"env": "lower", "default": "./data/fixture"}]}), "data.paths")
life_refused("data.paths env must match the compose file's variable",
             life(data={"paths": [{"env": "OTHER_DIR", "default": "./data/fixture"}]}), "data.paths")
life_refused("data.paths entry needs a default", life(data={"paths": [{"env": "FIXTURE_DATA_DIR"}]}),
             "data.paths")
life_refused("data.paths entry with an unknown key fails",
             life(data={"paths": [{"default": "./data/plain", "mode": "rw"}]}), "data.paths")
life_refused("data.paths may not be empty", life(data={"paths": []}), "data.paths")
life_refused("data with an unknown key fails", life(data={"files": []}), "data has unknown")
life_refused("data.volumes must be distinct compose names",
             life(data={"volumes": ["a", "a"]}), "data.volumes")
life_refused("data is built-in only", packaged(life(data={"volumes": ["a"]})), "data", "official")
check("data.paths is not checked against compose when there is no repo",
      am.validate(life(data={"paths": [{"default": "./data/unused"}]}), "builtin", repo=None) == [],
      str(am.validate(life(data={"paths": [{"default": "./data/unused"}]}), "builtin", repo=None)))

life_refused("onToggle.recreate may not be empty", life(onToggle={"recreate": []}), "onToggle.recreate")
life_refused("onToggle needs recreate", life(onToggle={"script": ["scripts/crm.sh"]}), "onToggle.recreate")
life_refused("onToggle.recreate entries are distinct",
             life(onToggle={"recreate": ["a", "a"]}), "onToggle.recreate")
life_refused("onToggle.script is a repo script",
             life(onToggle={"recreate": ["a"], "script": ["bin/x"]}), "onToggle.script")
life_refused("onToggle with an unknown key fails",
             life(onToggle={"recreate": ["a"], "stop": []}), "onToggle has unknown")

life_refused("backup.secrets entries are env names", life(backup={"secrets": ["lower"]}), "backup.secrets")
life_refused("backup.recommended entries are env names",
             life(backup={"recommended": ["lower"]}), "backup.recommended")
life_refused("backup with an unknown key fails", life(backup={"keys": []}), "backup has unknown")
check("backup is allowed from a community feed",
      life_errors(packaged(life(backup={"secrets": ["A_B"]})), "community") == [],
      str(life_errors(packaged(life(backup={"secrets": ["A_B"]})), "community")))

FIXTURE_DIR = ROOT / "scripts" / "fixtures" / "addons-cli"
fixture_manifest = json.loads((FIXTURE_DIR / "fixture" / "addon.json").read_text())
fixture_errors = am.validate(fixture_manifest, "builtin", repo=FIXTURE_DIR)
check("the addons-cli fixture is valid against its compose file", fixture_errors == [],
      str(fixture_errors))
check("the fixture declares seed last, as a oneshot",
      list(fixture_manifest["services"])[-1] == "seed"
      and fixture_manifest["services"]["seed"].get("oneshot") is True)
shutil.rmtree(LIFE_REPO)


def service_block(name: str) -> str:
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", compose, re.M | re.S)
    return m.group(1) if m else ""


def mount_problems(sid: str, manifest: dict, compose_text: str) -> list[str]:
    """Each service with `mounts` must mount exactly the fixed container paths
    from data/addons/<sid>/ with the declared mode."""
    problems = []
    for name, svc in manifest["services"].items():
        mounts = svc.get("mounts")
        if not mounts:
            continue
        m = re.search(rf"^  {re.escape(svc['compose'])}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
                      compose_text, re.M | re.S)
        block = m.group(1) if m else ""
        lines = re.findall(r"^      - (\S*/addons/\S+:/run/addon/\S+)$", block, re.M)
        want = [f"${{ASSISTANT_DATA_ROOT:-./data}}/addons/{sid}/{k}:{am.MOUNT_PATHS[k]}:{mode}"
                for k, mode in mounts.items()]
        if sorted(lines) != sorted(want):
            problems.append(f"{name}: compose mounts {sorted(lines)}, manifest wants {sorted(want)}")
    return problems


def compose_volumes(compose_text: str) -> set[str]:
    """Names under the top-level volumes: key of a compose file."""
    m = re.search(r"^volumes:\n(.*?)(?=^\S|\Z)", compose_text, re.M | re.S)
    return set(re.findall(r"^  ([A-Za-z0-9][A-Za-z0-9_.-]*):", m.group(1), re.M)) if m else set()


def volume_problems(manifest: dict, compose_text: str) -> list[str]:
    declared = compose_volumes(compose_text)
    return [f"volume {v} is not in docker-compose.yml's volumes"
            for v in am.data_volumes(manifest) if v not in declared]


FIX_VOLUMES = "services:\n  x:\n    image: x\nvolumes:\n  gptr_outputs:\n  other:\n"
check("volumes cross-check accepts a declared volume", volume_problems(volumed, FIX_VOLUMES) == [],
      str(volume_problems(volumed, FIX_VOLUMES)))
check("volumes cross-check flags an undeclared volume",
      len(volume_problems(volumed, FIX_VOLUMES.replace("gptr_outputs", "renamed"))) == 1)
check("volumes cross-check reads only the top-level volumes key",
      len(volume_problems(volumed, "services:\n  x:\n    volumes:\n      - gptr_outputs:/x\n")) == 1)


FIX_COMPOSE = """services:
  gptr-worker:
    profiles: ["gptr"]
    volumes:
      - ${ASSISTANT_DATA_ROOT:-./data}/addons/gptr/secrets:/run/addon/secrets:ro
      - ${ASSISTANT_DATA_ROOT:-./data}/addons/gptr/config:/run/addon/config:ro
      - ${ASSISTANT_DATA_ROOT:-./data}/addons/gptr/instances:/run/addon/instances:rw
      - ./other:/other
  next:
    image: x
"""
check("the config mount is the config directory", am.MOUNT_PATHS["config"] == "/run/addon/config",
      str(am.MOUNT_PATHS))
check("mounts cross-check accepts a matching compose service", mount_problems("gptr", rich, FIX_COMPOSE) == [],
      str(mount_problems("gptr", rich, FIX_COMPOSE)))
check("mounts cross-check flags the bare ./data form",
      len(mount_problems("gptr", rich, FIX_COMPOSE.replace("${ASSISTANT_DATA_ROOT:-./data}", "./data"))) == 1)
check("mounts cross-check flags a wrong mode",
      len(mount_problems("gptr", rich, FIX_COMPOSE.replace("instances:rw", "instances:ro"))) == 1)
check("mounts cross-check flags a missing mount",
      len(mount_problems("gptr", rich, FIX_COMPOSE.replace(
          "      - ${ASSISTANT_DATA_ROOT:-./data}/addons/gptr/secrets:/run/addon/secrets:ro\n", ""))) == 1)
check("mounts cross-check flags a wrong source directory",
      len(mount_problems("gptr", rich, FIX_COMPOSE.replace("addons/gptr/secrets", "addons/other/secrets"))) == 1)

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
    check(f"{sid}: compose mounts match services.*.mounts", not mount_problems(sid, manifest, compose),
          "; ".join(mount_problems(sid, manifest, compose)))
    check(f"{sid}: services.*.volumes are declared in docker-compose.yml",
          not volume_problems(manifest, compose), "; ".join(volume_problems(manifest, compose)))
    for g in manifest.get("gates", []):
        check(f"{sid}: sync-profiles maps gate {g['flag']} to {g['profile']}",
              [g["flag"], g["profile"], "1" if g["seed"] else "0"] in rows, table.stdout)
        check(f"{sid}: gate profile {g['profile']} exists in docker-compose.yml",
              bool(re.search(rf'^    profiles: \[(?:.*[ ,"])?"?{re.escape(g["profile"])}"?[,\]]', compose, re.M)))
    if manifest.get("mcp"):
        for var in [b["flag"], *manifest["mcp"].get("env", {}).values()]:
            n = len(re.findall(rf"^      - {var}=", compose, re.M))
            check(f"{sid}: both hermes-*-bootstrap services receive {var}", n == 2, f"found {n}")
    if manifest.get("vaultBackend"):
        # Both bootstraps read the flag for vault-plan; hermes reads it to install the plugin.
        n = len(re.findall(rf"^      - {b['flag']}=", compose, re.M))
        check(f"{sid}: both hermes-*-bootstrap services and hermes receive {b['flag']}", n == 3, f"found {n}")

for svc in ("hermes-api-bootstrap", "hermes-browser-bootstrap"):
    block = service_block(svc)
    check(f"{svc} mounts addons/", "- ./addons:/bootstrap/addons:ro" in block)
    check(f"{svc} mounts addon_manifest.py",
          "- ./scripts/lib/addon_manifest.py:/bootstrap/addon_manifest.py:ro" in block)

if failures:
    print(f"{failures} failed")
    sys.exit(1)
print("all add-on manifest checks passed")
