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
refused("core name collision", variant(mcp__name="messages"), "builtin", "collides")
check("mail and drafts are add-on names now (email-local), not core ones",
      not {"mail", "drafts"} & am.CORE_MCP_NAMES, str(am.CORE_MCP_NAMES))
refused("core prefix collision", variant(mcp__name="caldav-main"), "builtin", "collides")
check("crm- is no longer a core prefix", "crm-" not in am.CORE_MCP_PREFIXES, str(am.CORE_MCP_PREFIXES))
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

# --- mcp.instances ------------------------------------------------------------
INST_MCP = {
    "instances": {
        "name": "crm-{slug}",
        "url": "{setting:url}/mcp",
        "access": "{setting:access}",
        "auth": {"header": "Authorization", "scheme": "Bearer",
                 "secret": {"ro": "key_ro", "rw": "key_rw"}},
        "gate": {"setting": "local", "flag": "TWENTY_LOCAL_ENABLED"},
    },
    "transport": "http", "timeout": 120, "connectTimeout": 30,
    "tools": ["search_things", "create_thing"], "pin": ["search_things"],
    "writeTools": ["create_thing"],
    "profiles": {"default": "rw", "browser": "ro"},
}
INST_SETTINGS = {
    "url": {"type": "url"},
    "access": {"type": "choice", "options": ["read", "write"], "default": "read"},
    "key_ro": {"type": "secret", "optional": True},
    "key_rw": {"type": "secret", "optional": True},
    "local": {"type": "bool", "default": False},
}


def inst_manifest(mcp_changes=None, instances_settings=None, **top):
    m = variant()
    m["gates"] = [{"flag": "TWENTY_LOCAL_ENABLED", "profile": "crm", "seed": False}]
    m["instances"] = {"name": "workspace", "settings": copy.deepcopy(instances_settings or INST_SETTINGS)}
    mcp = copy.deepcopy(INST_MCP)
    for dotted, value in (mcp_changes or {}).items():
        node = mcp
        keys = dotted.split("__")
        for k in keys[:-1]:
            node = node[k]
        if value is None:
            node.pop(keys[-1], None)
        else:
            node[keys[-1]] = value
    m["mcp"] = mcp
    for k, v in top.items():
        if v is None:
            m.pop(k, None)
        else:
            m[k] = v
    return m


check("mcp.instances manifest is valid as built-in", am.validate(inst_manifest(), "builtin") == [],
      str(am.validate(inst_manifest(), "builtin")))
check("mcp.instances takes env and no service, path or name",
      am.validate(inst_manifest({"env": {"timeout": "TWENTY_MCP_TIMEOUT"}}), "builtin") == [],
      str(am.validate(inst_manifest({"env": {"timeout": "TWENTY_MCP_TIMEOUT"}}), "builtin")))
check("mcp.instances may gate on builtin.flag",
      am.validate(inst_manifest({"instances__gate__flag": "GPTR_MCP_ENABLED"}), "builtin") == [])
check("mcp.instances gate is optional",
      am.validate(inst_manifest({"instances__gate": None}), "builtin") == [])
refused("mcp.instances name without {slug}", inst_manifest({"instances__name": "crm"}),
        "builtin", "mcp.instances.name")
refused("mcp.instances name needs a lowercase prefix", inst_manifest({"instances__name": "{slug}"}),
        "builtin", "mcp.instances.name")
refused("mcp.instances name with a bad character", inst_manifest({"instances__name": "CRM-{slug}"}),
        "builtin", "mcp.instances.name")
refused("mcp.instances name with {slug} twice", inst_manifest({"instances__name": "a-{slug}-{slug}"}),
        "builtin", "mcp.instances.name")
refused("mcp.instances prefix collides with a core name", inst_manifest({"instances__name": "sql-{slug}"}),
        "builtin", "collides")
refused("mcp.instances prefix collides with a core prefix", inst_manifest({"instances__name": "caldav-{slug}"}),
        "builtin", "collides")
# The bootstrap drops every entry under an instance prefix that has no
# instance, so a prefix that overlaps a core name or prefix in either
# direction would drop core entries (sql, searxng, caldav-*).
for bad_name in ("s{slug}", "sq{slug}", "searx{slug}", "cal{slug}", "caldav-x-{slug}",
                 "messag{slug}", "messages-{slug}"):
    refused(f"mcp.instances prefix overlapping a core name or prefix ({bad_name})",
            inst_manifest({"instances__name": bad_name}), "builtin", "collides with a server core registers")
check("mcp.instances prefix crm- is accepted", am.validate(inst_manifest(), "builtin") == [])
check("mcp.instances prefix searx- is accepted (no core name starts with it)",
      am.validate(inst_manifest({"instances__name": "searx-{slug}"}), "builtin") == [])
refused("mcp.instances url naming an unknown setting",
        inst_manifest({"instances__url": "{setting:host}/mcp"}), "builtin", "mcp.instances.url")
refused("mcp.instances url without a setting",
        inst_manifest({"instances__url": "http://example.com/mcp"}), "builtin", "mcp.instances.url")
refused("mcp.instances url naming a bool setting",
        inst_manifest({"instances__url": "{setting:local}/mcp"}), "builtin", "mcp.instances.url")
refused("mcp.instances access that is not a setting reference",
        inst_manifest({"instances__access": "read"}), "builtin", "mcp.instances.access")
refused("mcp.instances access naming an unknown setting",
        inst_manifest({"instances__access": "{setting:level}"}), "builtin", "mcp.instances.access")
refused("mcp.instances access naming a non-choice setting",
        inst_manifest({"instances__access": "{setting:url}"}), "builtin", "mcp.instances.access")
refused("mcp.instances access with an option outside read and write",
        inst_manifest(instances_settings={**INST_SETTINGS, "access": {
            "type": "choice", "options": ["read", "write", "ask"], "default": "read"}}),
        "builtin", "mcp.instances.access")
check("mcp.instances access may offer read only",
      am.validate(inst_manifest(instances_settings={**INST_SETTINGS, "access": {
          "type": "choice", "options": ["read"], "default": "read"}}), "builtin") == [])
for bad_opts in ([["x"]], [{}], "read", None):
    refused(f"mcp.instances access with options {bad_opts!r} is an error, not a crash",
            inst_manifest(instances_settings={**INST_SETTINGS, "access": {
                "type": "choice", "options": bad_opts, "default": "read"}}),
            "builtin", "mcp.instances.access")
cron_ro = inst_manifest(permissions={**BASE["permissions"], "cron": "ro"})
check("cron ro is accepted with mcp.instances whose auth.secret has ro",
      am.validate(cron_ro, "builtin") == [], str(am.validate(cron_ro, "builtin")))
cron_ro_no_key = inst_manifest({"instances__auth__secret": {"rw": "key_rw"}},
                               permissions={**BASE["permissions"], "cron": "ro"})
refused("cron ro is refused when mcp.instances auth.secret has no ro", cron_ro_no_key, "builtin",
        'permissions.cron "ro"')
refused("mcp.instances auth.secret.ro naming an unknown setting",
        inst_manifest({"instances__auth__secret__ro": "nope"}), "builtin", "mcp.instances.auth.secret.ro")
refused("mcp.instances auth.secret.rw naming a non-secret setting",
        inst_manifest({"instances__auth__secret__rw": "url"}), "builtin", "mcp.instances.auth.secret.rw")
refused("mcp.instances auth.secret needs both levels",
        inst_manifest({"instances__auth__secret": {"ro": "key_ro"}}), "builtin", "mcp.instances.auth.secret")
refused("mcp.instances auth.secret with an extra level",
        inst_manifest({"instances__auth__secret__admin": "key_rw"}), "builtin", "mcp.instances.auth.secret")
refused("mcp.instances auth.header must be a header name",
        inst_manifest({"instances__auth__header": "Bad Header"}), "builtin", "mcp.instances.auth.header")
refused("mcp.instances auth unknown key",
        inst_manifest({"instances__auth__extra": "x"}), "builtin", "mcp.instances.auth")
refused("mcp.instances auth is required", inst_manifest({"instances__auth": None}),
        "builtin", "mcp.instances.auth")
refused("mcp.instances gate.setting naming a non-bool setting",
        inst_manifest({"instances__gate__setting": "url"}), "builtin", "mcp.instances.gate.setting")
refused("mcp.instances gate.setting naming an unknown setting",
        inst_manifest({"instances__gate__setting": "nope"}), "builtin", "mcp.instances.gate.setting")
refused("mcp.instances gate.flag that is neither builtin.flag nor a gate",
        inst_manifest({"instances__gate__flag": "OTHER_ENABLED"}), "builtin", "mcp.instances.gate.flag")
refused("mcp.instances gate unknown key",
        inst_manifest({"instances__gate__extra": 1}), "builtin", "mcp.instances.gate")
refused("mcp.instances unknown key", inst_manifest({"instances__colour": "red"}),
        "builtin", "mcp.instances has unknown fields")
refused("mcp.instances without a top-level instances block", inst_manifest(instances=None),
        "builtin", "needs a top-level instances block")
refused("mcp.instances alongside mcp.service",
        inst_manifest({"service": "server"}), "builtin", "mcp.instances")
refused("mcp.instances alongside mcp.path", inst_manifest({"path": "/mcp"}), "builtin", "mcp.instances")
refused("mcp.instances alongside mcp.name", inst_manifest({"name": "twenty"}), "builtin", "mcp.instances")
refused("mcp.instances needs transport", inst_manifest({"transport": None}), "builtin", "mcp.transport")
refused("mcp.instances needs tools", inst_manifest({"tools": None}), "builtin", "mcp.tools")
refused("mcp.instances needs profiles", inst_manifest({"profiles": None}), "builtin", "mcp.profiles")
refused("mcp.instances pin outside tools", inst_manifest({"pin": ["nope"]}), "builtin", "mcp.pin")
inst_packaged = packaged(inst_manifest({"instances__gate": None, "env": None}, gates=None))
refused("mcp.instances is built-in only", inst_packaged, "official",
        "mcp.instances is for built-in add-ons only")
refused("mcp.instances not an object", inst_manifest({"instances": "crm-{slug}"}),
        "builtin", "mcp.instances must be an object")

# Add-ons with mcp.instances have no name or URL of their own yet, so the
# plan and the cron fence skip them without failing.
with tempfile.TemporaryDirectory() as tmp:
    for sid, manifest in (("gptr", BASE), ("twentyish", None)):
        (Path(tmp) / sid).mkdir()
    (Path(tmp) / "gptr" / "addon.json").write_text(am.canonical(BASE))
    im = inst_manifest()
    im["id"] = "io.example.twentyish"
    im["permissions"]["cron"] = False
    (Path(tmp) / "twentyish" / "addon.json").write_text(am.canonical(im))
    check("the instances manifest passes check_builtin_file",
          am.check_builtin_file(Path(tmp) / "twentyish" / "addon.json", im, None) == [],
          str(am.check_builtin_file(Path(tmp) / "twentyish" / "addon.json", im, None)))
    plan = am.mcp_plan(Path(tmp), "default", {"GPTR_MCP_ENABLED": "1", "GPTR_EXTRA_ENABLED": "1"})
    check("mcp-plan emits a line for gptr only", len(plan) == 1 and plan[0].startswith("gptr|"), str(plan))
    got = am.cron_fence(Path(tmp), {})
    check("cron fence tolerates an instances manifest with cron false", got == ([], []), str(got))

# --- instances-plan (Twenty's real manifest, a synthetic store) ---------------
# Fields: name|title|on|url|keyenv|keyfile|timeout|connect|pins|reason|header|scheme.
# A line whose name ends in * describes the add-on: on is its flag, pins the bare
# pin names, reason the flag's name.
TW_PINS = ("get_tool_catalog learn_tools execute_tool list_object_metadata_names "
           "list_skills load_skills search_help_center")
FAKE_KEYS = {"main.key_ro": "ro-fake-main", "main.key_rw": "rw-fake-main",
             "remote.key_rw": "rw-fake-remote", "lookup.key_ro": "ro-fake-lookup",
             "lookup.key_rw": "rw-fake-lookup", "rdonly.key_rw": "rw-fake-rdonly",
             "odd.key_ro": "ro-fake-odd", "odd.key_rw": "rw-fake-odd",
             "badkey.key_rw": "rw-fake\nline", "withmcp.key_rw": "rw-fake-withmcp",
             "creds.key_rw": "rw-fake-creds"}


def tw_store(tmp: Path, instances: dict) -> Path:
    """data dir with addons-data/twenty/{instances,secrets}; returns the data dir."""
    data = tmp / "addon-data"
    (data / "twenty" / "instances").mkdir(parents=True)
    (data / "twenty" / "secrets").mkdir()
    for slug, values in instances.items():
        (data / "twenty" / "instances" / f"{slug}.json").write_text(json.dumps(values))
    for name, value in FAKE_KEYS.items():
        if name.split(".")[0] in instances:
            (data / "twenty" / "secrets" / name).write_text(value)
    return data


def plan_rows(data: Path, profile: str, environ: dict) -> dict[str, list[str]]:
    lines = am.instances_plan(ROOT / "addons", data, profile, environ)
    rows = {}
    for line in lines:
        fields = line.split("|")
        check(f"instances-plan line has 12 fields ({fields[0]})", len(fields) == 12, line)
        rows[fields[0]] = fields
    return rows


TW_ON = {"TWENTY_MCP_ENABLED": "1", "TWENTY_LOCAL_ENABLED": "1"}
with tempfile.TemporaryDirectory() as tmp:
    data = tw_store(Path(tmp), {
        "main": {"url": "http://twenty-server:3000", "access": "write", "local": True},
        "remote": {"url": "https://crm.example.com/", "access": "write", "local": False},
        "lookup": {"url": "https://lookup.example.com", "access": "read"},
        "rdonly": {"url": "https://rdonly.example.com", "access": "read"},
        "odd": {"url": "https://odd.example.com", "access": "admin"},
        "ftp": {"url": "ftp://crm.example.com", "access": "write"},
        "badkey": {"url": "https://badkey.example.com", "access": "write"},
        "Bad_Slug": {"url": "https://bad.example.com", "access": "write"},
        "withmcp": {"url": "https://crm.example.com/mcp/", "access": "write"},
        "creds": {"url": "https://user:pw@crm.example.com", "access": "write"},
    })
    (data / "twenty" / "instances" / ".hidden.json").write_text("{}")
    (data / "twenty" / "instances" / "sample.json.example").write_text("{}")
    out = "\n".join(am.instances_plan(ROOT / "addons", data, "default", TW_ON))
    check("instances-plan never prints a key value", "fake" not in out, out)

    rows = plan_rows(data, "default", TW_ON)
    check("the add-on line names the prefix, flag and bare pins",
          rows.get("crm-*", [])[:3] == ["crm-*", "Twenty CRM", "1"]
          and rows["crm-*"][8] == TW_PINS and rows["crm-*"][9] == "TWENTY_MCP_ENABLED", str(rows.get("crm-*")))
    m = rows.get("crm-main", [])
    check("default: crm-main is on at twenty-server/mcp with CRM_MAIN_KEY and key_rw",
          m[2:6] == ["1", "http://twenty-server:3000/mcp", "CRM_MAIN_KEY",
                     str(data / "twenty" / "secrets" / "main.key_rw")], str(m))
    check("crm-main carries timeouts, the seven pins and the Bearer header",
          m[6:8] == ["60", "30"] and m[8] == TW_PINS and m[9] == "" and m[10:] == ["Authorization", "Bearer"], str(m))
    check("a trailing slash on the url is dropped",
          rows.get("crm-remote", [""] * 4)[3] == "https://crm.example.com/mcp", str(rows.get("crm-remote")))
    check("access read gives default the ro key",
          rows.get("crm-lookup", [""] * 6)[5].endswith("lookup.key_ro"), str(rows.get("crm-lookup")))
    r = rows.get("crm-rdonly", [])
    check("access read with no key_ro: off, reason names key_ro",
          r[2:3] == ["0"] and r[5] == "" and r[9].startswith("key ") and "key_ro" in r[9], str(r))
    o = rows.get("crm-odd", [])
    check("unknown access: off with an access reason that holds no value",
          o[2:3] == ["0"] and o[9].startswith("access ") and "admin" not in o[9], str(o))
    f = rows.get("crm-ftp", [])
    check("a non-http url is off", f[2:3] == ["0"] and f[9].startswith("url "), str(f))
    w = rows.get("crm-withmcp", [])
    check("a stored url already ending in /mcp is not doubled",
          w[2:4] == ["1", "https://crm.example.com/mcp"], str(w))
    c = rows.get("crm-creds", [])
    check("a url with a user name or password is off, and the reason holds no URL",
          c[2:4] == ["0", ""] and c[9].startswith("url ") and "example" not in c[9] and "pw" not in c[9], str(c))
    check("instances-plan never prints a url's password", "pw@" not in out, out)
    b = rows.get("crm-badkey", [])
    check("a key holding a line break is off", b[2:3] == ["0"] and b[9].startswith("key "), str(b))
    check("bad slugs and hidden files get no line",
          not any(k in rows for k in ("crm-Bad_Slug", "crm-.hidden", "crm-sample")), str(sorted(rows)))

    rows = plan_rows(data, "api-server", TW_ON)
    check("api-server: crm-main uses key_ro",
          rows.get("crm-main", [""] * 6)[2] == "1" and rows["crm-main"][5].endswith("main.key_ro"),
          str(rows.get("crm-main")))
    r = rows.get("crm-remote", [])
    check("api-server: remote without key_ro is off, reason key", r[2:3] == ["0"] and r[9].startswith("key "), str(r))
    check("api-server: access read keeps key_ro",
          rows.get("crm-lookup", [""] * 6)[5].endswith("lookup.key_ro"), str(rows.get("crm-lookup")))
    check("api-server: no line hands out a key_rw",
          not any(r[5].endswith(".key_rw") for r in rows.values()), str(rows))
    check("api-server: unknown access is off too", rows.get("crm-odd", ["", "", "1"])[2] == "0")

    rows = plan_rows(data, "browser", TW_ON)
    check("browser follows access", rows.get("crm-main", [""] * 6)[5].endswith("main.key_rw")
          and rows.get("crm-lookup", [""] * 6)[5].endswith("lookup.key_ro"), str(rows))

    rows = plan_rows(data, "worker", TW_ON)
    check("a profile outside mcp.profiles gets no entry, reason profile",
          rows.get("crm-main", [""] * 10)[2] == "0" and rows["crm-main"][9].startswith("profile "),
          str(rows.get("crm-main")))

    local_off = {"TWENTY_MCP_ENABLED": "1", "TWENTY_LOCAL_ENABLED": "0"}
    rows = plan_rows(data, "default", local_off)
    check("local instance skipped while TWENTY_LOCAL_ENABLED=0, reason gate",
          rows.get("crm-main", [""] * 10)[2] == "0" and rows["crm-main"][9].startswith("gate "),
          str(rows.get("crm-main")))
    check("remote instance kept while the local one is off", rows.get("crm-remote", ["", "", "0"])[2] == "1")
    rows = plan_rows(data, "default", {"TWENTY_MCP_ENABLED": "1"})
    check("an unset gate flag is off", rows.get("crm-main", ["", "", "1"])[2] == "0")

    rows = plan_rows(data, "default", {"TWENTY_MCP_ENABLED": "0", "TWENTY_LOCAL_ENABLED": "1"})
    check("TWENTY_MCP_ENABLED=0: the add-on line is off and every instance is off with reason flag",
          rows.get("crm-*", ["", "", "1"])[2] == "0"
          and all(r[2] == "0" and r[9].startswith("flag ") for k, r in rows.items() if k != "crm-*"),
          str(rows))
    rows = plan_rows(data, "default", {**TW_ON, "TWENTY_MCP_TIMEOUT": "90"})
    check("TWENTY_MCP_TIMEOUT overrides the timeout", rows.get("crm-main", [""] * 7)[6] == "90")

# Values of the wrong JSON type in an instance file: that instance is off with
# a reason, the others still register, and nothing raises.
with tempfile.TemporaryDirectory() as tmp:
    FAKE_KEYS.update({"listacc.key_rw": "rw-fake-l", "dictacc.key_rw": "rw-fake-d",
                      "strlocal.key_rw": "rw-fake-s", "listlocal.key_rw": "rw-fake-ll",
                      "listurl.key_rw": "rw-fake-lu"})
    data = tw_store(Path(tmp), {
        "remote": {"url": "https://crm.example.com", "access": "write", "local": False},
        "listacc": {"url": "https://crm.example.com", "access": ["write"]},
        "dictacc": {"url": "https://crm.example.com", "access": {"write": 1}},
        "strlocal": {"url": "http://twenty-server:3000", "access": "write", "local": "true"},
        "listlocal": {"url": "http://twenty-server:3000", "access": "write", "local": [1]},
        "listurl": {"url": ["https://crm.example.com"], "access": "write"},
    })
    try:
        rows = plan_rows(data, "default", {"TWENTY_MCP_ENABLED": "1", "TWENTY_LOCAL_ENABLED": "0"})
    except Exception as e:  # noqa: BLE001
        rows = {}
        check("instances-plan survives ill-typed instance values", False, f"{type(e).__name__}: {e}")
    check("a good instance still registers beside ill-typed ones",
          rows.get("crm-remote", ["", "", "0"])[2] == "1", str(rows.get("crm-remote")))
    for slug in ("listacc", "dictacc"):
        r = rows.get(f"crm-{slug}", [""] * 10)
        check(f"access that isn't a string is off, reason access ({slug})",
              r[2] == "0" and r[9].startswith("access "), str(r))
    for slug in ("strlocal", "listlocal"):
        r = rows.get(f"crm-{slug}", [""] * 10)
        check(f"a gate setting that isn't a bool is off, reason gate ({slug})",
              r[2] == "0" and r[9].startswith("gate "), str(r))
    r = rows.get("crm-listurl", [""] * 10)
    check("a url that isn't a string is off, reason url", r[2] == "0" and r[9].startswith("url "), str(r))
    rows = plan_rows(data, "default", TW_ON)
    for slug in ("strlocal", "listlocal"):
        r = rows.get(f"crm-{slug}", [""] * 10)
        check(f"a non-bool gate setting stays off with the gate flag on ({slug})",
              r[2] == "0" and r[9].startswith("gate "), str(r))

with tempfile.TemporaryDirectory() as tmp:
    rows = plan_rows(Path(tmp) / "missing", "default", TW_ON)
    check("no store: only the add-on line", sorted(rows) == ["crm-*"], str(sorted(rows)))
    check("main() prints instances-plan",
          subprocess.run([sys.executable, str(ROOT / "scripts/lib/addon_manifest.py"), "instances-plan",
                          str(ROOT / "addons"), str(Path(tmp) / "missing"), "default"],
                         capture_output=True, text=True, env={"TWENTY_MCP_ENABLED": "1"}).stdout.startswith("crm-*|"))

# A broken manifest with mcp.instances still names its prefix, off, so the
# bootstrap drops its entries.
with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "twenty").mkdir()
    broken = json.loads((ROOT / "addons" / "twenty" / "addon.json").read_text())
    broken["permissions"]["sends"] = "maybe"
    (Path(tmp) / "twenty" / "addon.json").write_text(am.canonical(broken))
    lines = am.instances_plan(Path(tmp), Path(tmp) / "data", "default", TW_ON)
    check("an invalid instances manifest gives an off add-on line",
          [l.split("|")[:3] for l in lines] == [["crm-*", "twenty", "0"]], str(lines))

# --- cross-manifest conflicts -------------------------------------------------
check("real addons/ has no cross-manifest conflict", am.dir_conflicts(am.load_dir(ROOT / "addons")) == {},
      str(am.dir_conflicts(am.load_dir(ROOT / "addons"))))


def conflict_dir(tmp: Path, extra: dict) -> Path:
    """Real addons/ plus extra {sid: manifest} in a scratch addons dir."""
    d = tmp / "addons"
    shutil.copytree(ROOT / "addons", d)
    for sid, m in extra.items():
        (d / sid).mkdir()
        (d / sid / "addon.json").write_text(am.canonical(m))
    return d


crmx = copy.deepcopy(BASE)
crmx["id"] = "io.example.crmx"
crmx["mcp"]["name"] = "crm-x"
check("crm-x alone is a valid manifest (crm- is not a core prefix)", am.validate(crmx, "builtin") == [],
      str(am.validate(crmx, "builtin")))
with tempfile.TemporaryDirectory() as tmp:
    d = conflict_dir(Path(tmp), {"crmx": crmx})
    errs = {p.parent.name: e for p, _m, e in am.checked_dir(d)}
    check("an mcp.name under Twenty's instance prefix is refused",
          any("crm-x" in e and "twenty" in e for e in errs["crmx"]), str(errs["crmx"]))
    check("Twenty itself stays valid next to it", errs["twenty"] == [], str(errs["twenty"]))
    plan = am.mcp_plan(d, "default", {"GPTR_MCP_ENABLED": "1"})
    check("mcp-plan skips the conflicting add-on", not any(l.startswith("crm-x|") for l in plan), str(plan))
    validate = subprocess.run([sys.executable, str(ROOT / "scripts/lib/addon_manifest.py"), "validate", str(d)],
                              capture_output=True, text=True)
    check("validate reports the conflict", validate.returncode == 1 and "crm-x" in validate.stdout,
          validate.stdout)

# mail and drafts left CORE_MCP_NAMES for email-local, so the cross-add-on
# check is what keeps another add-on's instance prefix off them.
mai = json.loads((ROOT / "addons" / "twenty" / "addon.json").read_text())
mai["id"] = "io.example.mai"
mai["mcp"]["instances"]["name"] = "mai{slug}"
check("instance prefix mai passes validate alone", am.validate(mai, "builtin") == [],
      str(am.validate(mai, "builtin")))
with tempfile.TemporaryDirectory() as tmp:
    d = conflict_dir(Path(tmp), {"mai": mai})
    errs = {p.parent.name: e for p, _m, e in am.checked_dir(d)}
    check("email-local's mail under another add-on's instance prefix is refused",
          any("'mail'" in e and "mai's instance prefix" in e for e in errs["email-local"]), str(errs))

overlap = json.loads((ROOT / "addons" / "twenty" / "addon.json").read_text())
overlap["id"] = "io.example.twentyish"
overlap["mcp"]["instances"]["name"] = "crm-eu-{slug}"
with tempfile.TemporaryDirectory() as tmp:
    d = conflict_dir(Path(tmp), {"twentyish": overlap})
    errs = {p.parent.name: e for p, _m, e in am.checked_dir(d)}
    check("overlapping instance prefixes refuse both add-ons",
          any("overlap" in e for e in errs["twentyish"]) and any("overlap" in e for e in errs["twenty"]),
          str(errs))
    lines = am.instances_plan(d, Path(tmp) / "data", "default", TW_ON)
    check("instances-plan turns both off, so their entries are dropped",
          sorted(l.split("|")[0] + ":" + l.split("|")[2] for l in lines) == ["crm-*:0", "crm-eu-*:0"], str(lines))

with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    for sid, m in (("crmx", crmx), ("twenty", json.loads((ROOT / "addons" / "twenty" / "addon.json").read_text()))):
        (d / sid).mkdir()
        (d / sid / "addon.json").write_text(am.canonical(m))
    conflicts = am.dir_conflicts(am.load_dir(d))
    check("synthetic dir: only the add-on with the name is refused",
          sorted(p.parent.name for p in conflicts) == ["crmx"], str(conflicts))
    same = json.loads((ROOT / "addons" / "twenty" / "addon.json").read_text())
    same["id"] = "io.example.twentyish"
    (d / "twentyish").mkdir()
    (d / "twentyish" / "addon.json").write_text(am.canonical(same))
    conflicts = am.dir_conflicts(am.load_dir(d))
    check("synthetic dir: the same prefix twice is an overlap",
          any("overlap" in e for e in conflicts.get(d / "twentyish" / "addon.json", [])), str(conflicts))

# Cron: Twenty declares cron true, which is what it does: crm-* entries stay
# in cron as before 0.3.13, and a job on default uses that profile's key (rw
# for a write workspace). With cron false every instance's entry is fenced.
TW_MANIFEST = json.loads((ROOT / "addons" / "twenty" / "addon.json").read_text())
check("twenty declares permissions.cron true", TW_MANIFEST["permissions"]["cron"] is True,
      repr(TW_MANIFEST["permissions"]["cron"]))
with tempfile.TemporaryDirectory() as tmp:
    data = tw_store(Path(tmp), {"main": {"url": "http://twenty-server:3000", "access": "write"},
                                "remote": {"url": "https://crm.example.com", "access": "read"}})
    got = am.cron_fence(ROOT / "addons", TW_ON, data)
    check("twenty (cron true): no crm-* name or instance URL is fenced",
          not any(n.startswith("crm-") for n in got[0])
          and not any("twenty" in u or "example.com" in u for u in got[1]), str(got))
    addons = Path(tmp) / "addons"
    (addons / "twenty").mkdir(parents=True)
    fenced = json.loads((ROOT / "addons" / "twenty" / "addon.json").read_text())
    fenced["permissions"]["cron"] = False
    (addons / "twenty" / "addon.json").write_text(am.canonical(fenced))
    got = am.cron_fence(addons, TW_ON, data)
    check("an instances add-on with cron false fences each instance's name",
          got[0] == ["crm-main", "crm-remote"], str(got))
    fenced["permissions"]["sends"] = "maybe"
    (addons / "twenty" / "addon.json").write_text(am.canonical(fenced))
    got = am.cron_fence(addons, TW_ON, data)
    check("an invalid instances manifest fences each instance's name",
          got[0] == ["crm-main", "crm-remote"], str(got))

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
    "      - ./data/par/child:/par\n"
    "      - ${TW_DIR:-./data/tw}/storage:/tw\n"
    "      - ./data/twenty/storage:/twl\n"
    "      - ${TW2_DIR:-./data/twenty}/storage:/twe\n"
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

# transient instance secrets and optional instances
TRANSIENT_INST = {"name": "w", "settings": {"url": {"type": "url"},
                  "password": {"type": "secret", "transient": True}},
                  "connect": CONNECT, "disconnect": CONNECT}
check("a transient instance secret with a connect hook is valid",
      life_errors(life(instances=TRANSIENT_INST)) == [], str(life_errors(life(instances=TRANSIENT_INST))))
no_hooks = copy.deepcopy(TRANSIENT_INST)
del no_hooks["connect"], no_hooks["disconnect"]
life_refused("transient without instances.connect fails", life(instances=no_hooks), "instances.settings.password.transient")
str_inst = copy.deepcopy(TRANSIENT_INST)
str_inst["settings"]["url"]["transient"] = True
life_refused("transient on a string setting fails", life(instances=str_inst), "instances.settings.url.transient")
life_refused("transient on a top-level setting fails",
             life(settings={"tok": {"type": "secret", "transient": True}}, instances=TRANSIENT_INST),
             "settings.tok.transient")
nb_inst = copy.deepcopy(TRANSIENT_INST)
nb_inst["settings"]["password"]["transient"] = "yes"
life_refused("transient must be a bool", life(instances=nb_inst), "instances.settings.password.transient")
life_refused("transient is built-in only",
             packaged(life(instances=TRANSIENT_INST)), "transient", "official")
opt = {"name": "w", "settings": {"url": {"type": "url"}}, "optional": True}
check("instances.optional true is valid", life_errors(life(instances=opt)) == [], str(life_errors(life(instances=opt))))
life_refused("instances.optional must be a bool",
             life(instances={**opt, "optional": "yes"}), "instances.optional")
life_refused("instances.optional is built-in only", packaged(life(instances=opt)), "instances.optional", "official")

for bad in ("../x", "/etc", "./data/../x", "./data", "./data/", "./data/unused",
            "./data/.", "./data//x", "./data/./", "./data/x/", "./data/./x", "./data/x/.."):
    life_refused(f"data.paths default {bad!r} fails", life(data={"paths": [{"default": bad}]}),
                 "data.paths")
check("a data.paths default that is the parent of a bind source passes",
      life_errors(life(data={"paths": [{"default": "./data/plain"}, {"default": "./data/par"},
                                       {"env": "TW_DIR", "default": "./data/tw"}]})) == [],
      str(life_errors(life(data={"paths": [{"default": "./data/par"}]}))))
life_refused("a parent match is on whole path segments",
             life(data={"paths": [{"default": "./data/pare"}]}), "data.paths")
life_refused("./data/twen is not a parent of the literal source ./data/twenty/storage",
             life(data={"paths": [{"default": "./data/twen"}]}), "data.paths")
life_refused("./data/twen is not a parent of ${TW2_DIR:-./data/twenty}/storage",
             life(data={"paths": [{"env": "TW2_DIR", "default": "./data/twen"}]}), "data.paths")
check("./data/twenty is a parent of both twenty/storage sources",
      life_errors(life(data={"paths": [{"default": "./data/twenty"},
                                       {"env": "TW2_DIR", "default": "./data/twenty"}]})) == [],
      str(life_errors(life(data={"paths": [{"default": "./data/twenty"}]}))))
life_refused("a parent match through the env form needs the same env",
             life(data={"paths": [{"env": "OTHER_DIR", "default": "./data/tw"}]}), "data.paths")
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


def volume_owner_problems(manifest: dict, compose_text: str) -> list[str]:
    """Compose services outside the add-on that mount one of its data volumes."""
    own = {svc["compose"] for svc in manifest["services"].values()}
    top = re.split(r"^volumes:\n", compose_text, maxsplit=1, flags=re.M)[0]
    problems = []
    for m in re.finditer(r"^  ([A-Za-z0-9_-]+):\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", top, re.M | re.S):
        if m.group(1) in own:
            continue
        for v in am.data_volumes(manifest):
            if re.search(rf"^      - [\"']?{re.escape(v)}:", m.group(2), re.M):
                problems.append(f"{m.group(1)} mounts {v}, which belongs to the add-on")
    return problems


own_svc = next(iter(volumed["services"].values()))["compose"]
FIX_OWNED = (f"services:\n  {own_svc}:\n    volumes:\n      - gptr_outputs:/o\n  core:\n    image: x\n"
             "volumes:\n  gptr_outputs:\n")
check("volume ownership accepts the add-on's own service", volume_owner_problems(volumed, FIX_OWNED) == [],
      str(volume_owner_problems(volumed, FIX_OWNED)))
shared = FIX_OWNED.replace("image: x", "volumes:\n      - gptr_outputs:/o")
check("volume ownership flags a core service that mounts the add-on's volume",
      len(volume_owner_problems(volumed, shared)) == 1, str(volume_owner_problems(volumed, shared)))

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
        # A service behind a gate runs under the gate's profile; builtin.profile
        # may then be a name no service uses (Twenty: crm-mcp).
        want_profile = b["profile"]
        if "when" in svc:
            want_profile = next(g["profile"] for g in manifest["gates"] if g["flag"] == svc["when"]["gate"])
        check(f"{sid}: {svc['compose']} is in the {want_profile} profile", want_profile in listed, str(listed))
    check(f"{sid}: compose mounts match services.*.mounts", not mount_problems(sid, manifest, compose),
          "; ".join(mount_problems(sid, manifest, compose)))
    check(f"{sid}: services.*.volumes are declared in docker-compose.yml",
          not volume_problems(manifest, compose), "; ".join(volume_problems(manifest, compose)))
    check(f"{sid}: data volumes are mounted only by its own compose services",
          not volume_owner_problems(manifest, compose), "; ".join(volume_owner_problems(manifest, compose)))
    for g in manifest.get("gates", []):
        check(f"{sid}: sync-profiles maps gate {g['flag']} to {g['profile']}",
              [g["flag"], g["profile"], "1" if g["seed"] else "0"] in rows, table.stdout)
        check(f"{sid}: gate profile {g['profile']} exists in docker-compose.yml",
              bool(re.search(rf'^    profiles: \[(?:.*[ ,"])?"?{re.escape(g["profile"])}"?[,\]]', compose, re.M)))
    if manifest.get("mcp"):
        entries = am.mcp_entries(manifest)
        bootstrap_vars = [b["flag"], *(e["when"]["gate"] for e in entries if "when" in e)]
        bootstrap_vars += [v for e in entries for v in e.get("env", {}).values()]
        for var in dict.fromkeys(bootstrap_vars):
            n = len(re.findall(rf"^      - {var}=", compose, re.M))
            check(f"{sid}: both hermes-*-bootstrap services receive {var}", n == 2, f"found {n}")
    if manifest.get("vaultBackend"):
        # Both bootstraps read the flag for vault-plan. hermes installs the plugin
        # whatever the flag says, so flipping it does not recreate hermes.
        n = len(re.findall(rf"^      - {b['flag']}=", compose, re.M))
        check(f"{sid}: both hermes-*-bootstrap services, and not hermes, receive {b['flag']}", n == 2, f"found {n}")

# --- Twenty ------------------------------------------------------------------
tw_path = ROOT / "addons" / "twenty" / "addon.json"
check("addons/twenty/addon.json exists", tw_path.is_file())
if tw_path.is_file():
    tw = json.loads(tw_path.read_text())
    check("twenty manifest is canonical JSON", tw_path.read_text() == am.canonical(tw))
    tw_default = tw["data"]["paths"][0]["default"]
    for svc_name in ("twenty-db", "twenty-server"):
        block = service_block(svc_name)
        want = "${TWENTY_DATA_DIR:-" + tw_default + "}/"
        check(f"data.paths[0].default matches the {svc_name} bind mount", want in block, want)
    check("twenty: builtin.profile is a name no compose service uses",
          not re.search(rf'^    profiles: \[(?:.*[ ,"])?"?{re.escape(tw["builtin"]["profile"])}"?[,\]]',
                        compose, re.M))
    check("twenty: TWENTY_LOCAL_ENABLED is a gate on the crm profile",
          {"flag": "TWENTY_LOCAL_ENABLED", "profile": "crm", "seed": False} in tw["gates"])
    check("twenty: every service is gated on TWENTY_LOCAL_ENABLED",
          all(s.get("when") == {"gate": "TWENTY_LOCAL_ENABLED"} for s in tw["services"].values()))
    check("twenty: the four compose services are listed",
          sorted(s["compose"] for s in tw["services"].values())
          == ["twenty-db", "twenty-redis", "twenty-server", "twenty-worker"])
    check("twenty: mcp.pin is the seven tools Twenty's MCP lists for every workspace",
          tw["mcp"]["pin"] == TW_PINS.split(), str(tw["mcp"]["pin"]))
    gate = tw["mcp"]["instances"]["gate"]["flag"]
    n = len(re.findall(rf"^      - {gate}=", compose, re.M))
    check(f"twenty: both hermes-*-bootstrap services receive the instance gate {gate}", n == 2, f"found {n}")
    bootstrap = (ROOT / "compose/hermes/bootstrap-api-profile.sh").read_text()
    check("the bootstrap has no Twenty-specific registration left",
          not re.search(r"register_twenty|reconcile_twenty|twenty_[a-z_]+\(\)|TWENTY_WORKSPACES_DIR|TWENTY_LOCAL_MCP_URL"
                        r"|TWENTY_MCP_PINS", bootstrap))

for svc in ("hermes-api-bootstrap", "hermes-browser-bootstrap"):
    block = service_block(svc)
    check(f"{svc} mounts addons/", "- ./addons:/bootstrap/addons:ro" in block)
    check(f"{svc} mounts addon_manifest.py",
          "- ./scripts/lib/addon_manifest.py:/bootstrap/addon_manifest.py:ro" in block)
    check(f"{svc} mounts the Twenty instance store read-only",
          "- ${ASSISTANT_DATA_ROOT:-./data}/addons/twenty:/bootstrap/addon-data/twenty:ro" in block)
    check(f"{svc} sets ADDON_DATA_DIR", "- ADDON_DATA_DIR=/bootstrap/addon-data" in block)
    check(f"{svc} no longer mounts the workspace files", "twenty-workspaces" not in block)

if failures:
    print(f"{failures} failed")
    sys.exit(1)
print("all add-on manifest checks passed")
