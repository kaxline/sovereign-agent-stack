#!/bin/sh
# Shape of the budget and budget-local profiles (Actual Budget) in
# docker-compose.yml, the flags that turn them on, and the files that keep the
# budget credentials out of git and out of container environments.
# Needs a .env that satisfies compose interpolation, as CI writes. Prints no
# secret values.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
cd "$ROOT"

bash scripts/sync-profiles.sh --table | grep -qx 'ACTUAL_LOCAL_ENABLED budget-local 0' \
  || fail "sync-profiles.sh maps ACTUAL_LOCAL_ENABLED to budget-local (the gate in addons/actual/addon.json)"

for k in ACTUAL_MCP_ENABLED ACTUAL_LOCAL_ENABLED ACTUAL_PORT ACTUAL_DATA_DIR ACTUAL_SERVER_IMAGE; do
  grep -qE "^#? ?${k}=" .env.example || fail ".env.example documents ${k}"
done
for k in ACTUAL_ACCESS ACTUAL_BUDGET_FILE; do
  if grep -qE "^#? ?${k}=" .env.example; then fail ".env.example still has the single budget's ${k}"; fi
done
grep -qE '^ACTUAL_MCP_ENABLED=0$' .env.example || fail ".env.example has ACTUAL_MCP_ENABLED=0"
grep -qE '^ACTUAL_LOCAL_ENABLED=0$' .env.example || fail ".env.example has ACTUAL_LOCAL_ENABLED=0"

# Nothing tracked is left under compose/actual/, and a budget file from before
# 0.3.15 stays gitignored.
[ -z "$(git ls-files compose/actual)" ] || fail "compose/actual/ has tracked files"
for f in compose/actual/budget.env compose/actual/other; do
  git check-ignore -q "$f" || fail "$f must be gitignored"
done
# The root .gitignore's data/ rule must not drop vendored sources.
if git check-ignore -q compose/actual-mcp/src/core/data/fetch-accounts.ts; then
  fail "compose/actual-mcp/src/core/data/ must stay tracked"
fi

# Vendored MCP server: pinned, npm packages instead of the author's local fork.
[ -f compose/actual-mcp/VENDORED.md ] || fail "compose/actual-mcp/VENDORED.md exists"
grep -qF '0c73b0d727a1d32bb924ab323985fffc9f0944fb' compose/actual-mcp/VENDORED.md \
  || fail "VENDORED.md names the upstream commit"
if grep -qF '"file:' compose/actual-mcp/package.json compose/actual-mcp/package-lock.json; then
  fail "compose/actual-mcp has a file: dependency"
fi
grep -qF '"@actual-app/api": "26.11.0"' compose/actual-mcp/package.json || fail "@actual-app/api is 26.11.0"
grep -qF '"@actual-app/core": "26.11.0"' compose/actual-mcp/package.json || fail "@actual-app/core is 26.11.0"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Only docker-compose.yml, with the default data root: a local override file or
# an ASSISTANT_DATA_ROOT in .env must not change what this checks (as in
# scripts/lib/compose_config.py).
compose_config() {
  ASSISTANT_DATA_ROOT=./data COMPOSE_PROFILES="$1" \
    docker compose -f docker-compose.yml config --format json
}

# With no profiles, no Actual service is defined.
compose_config "" > "${tmp}/none.json" \
  || fail "docker compose config with no profiles"
compose_config core,budget,budget-local > "${tmp}/budget.json" \
  || fail "docker compose config with core,budget,budget-local"

python3 - "${tmp}/none.json" "${tmp}/budget.json" <<'EOF'
import json, sys

none = json.load(open(sys.argv[1]))
cfg = json.load(open(sys.argv[2]))
errors = []
ACTUAL = ("actual-server", "actual-mcp-rw", "actual-mcp-ro")
DIGEST = "sha256:06cf186569cf58d93e12659ff1a517b65b5b0ebee7050a3991d4bd8a1d398da6"


def env_map(service):
    raw = service.get("environment") or {}
    if isinstance(raw, dict):
        return dict(raw)
    return dict(item.split("=", 1) if "=" in item else (item, None) for item in raw)


for name in ACTUAL:
    if name in (none.get("services") or {}):
        errors.append(f"{name} is defined with no profiles")

services = cfg.get("services") or {}
missing = [n for n in ACTUAL if n not in services]
if missing:
    print(f"FAIL missing services {missing}", file=sys.stderr)
    raise SystemExit(1)

server = services["actual-server"]
if server.get("profiles") != ["budget-local"]:
    errors.append(f"actual-server profiles are {server.get('profiles')}, want ['budget-local']")
if server.get("image") != f"actualbudget/actual-server:26.11.0@{DIGEST}":
    errors.append(f"actual-server image is {server.get('image')}")
ports = server.get("ports") or []
if not ports:
    errors.append("actual-server publishes no port")
for port in ports:
    if port.get("host_ip") != "127.0.0.1":
        errors.append(f"actual-server publishes {port.get('target')} without a 127.0.0.1 bind")
    if str(port.get("target")) != "5006" or str(port.get("published")) != "5006":
        errors.append(f"actual-server publishes {port.get('published')}:{port.get('target')}, want 5006:5006")
if not any(v.get("target") == "/data" for v in server.get("volumes") or []):
    errors.append("actual-server does not mount /data")
if set(server.get("networks") or {}) != {"budget"}:
    errors.append(f"actual-server networks are {sorted(server.get('networks') or {})}, want ['budget']")

for name, write in (("actual-mcp-rw", True), ("actual-mcp-ro", False)):
    svc = services[name]
    if svc.get("profiles") != ["budget"]:
        errors.append(f"{name} profiles are {svc.get('profiles')}, want ['budget']")
    if svc.get("ports"):
        errors.append(f"{name} publishes a port")
    if svc.get("env_file"):
        errors.append(f"{name} has an env_file")
    if not str(svc.get("image", "")).endswith("-actual-mcp:local"):
        errors.append(f"{name} image is {svc.get('image')}")
    if not str((svc.get("build") or {}).get("context", "")).endswith("compose/actual-mcp"):
        errors.append(f"{name} does not build compose/actual-mcp")
    cmd = svc.get("command") or []
    if "--sse" not in cmd:
        errors.append(f"{name} command lacks --sse")
    if ("--enable-write" in cmd) != write:
        errors.append(f"{name} command {'lacks' if write else 'has'} --enable-write")
    if "--enable-bearer" in cmd:
        errors.append(f"{name} turns on bearer auth with no token wired")
    env = env_map(svc)
    if env != {"ACTUAL_SYNC_TTL_MS": "0", "ACTUAL_DATA_DIR": "/data"}:
        errors.append(f"{name} environment keys are {sorted(env)}, want ACTUAL_SYNC_TTL_MS=0 and ACTUAL_DATA_DIR=/data")
    if svc.get("secrets"):
        errors.append(f"{name} still mounts a secret ({svc.get('secrets')})")
    cache = name.replace("-", "_") + "_cache"
    vols = svc.get("volumes") or []
    if not any(v.get("type") == "volume" and v.get("source") == cache and v.get("target") == "/data" for v in vols):
        errors.append(f"{name} does not mount volume {cache} at /data")
    rows = [v for v in vols if v.get("target") == "/budgets"]
    if len(rows) != 1 or rows[0].get("type") != "bind" or not rows[0].get("read_only") \
            or not str(rows[0].get("source", "")).endswith("/data/addons/actual"):
        errors.append(f"{name} does not mount <data root>/addons/actual read-only at /budgets ({rows})")
    if set(svc.get("networks") or {}) != {"budget"}:
        errors.append(f"{name} networks are {sorted(svc.get('networks') or {})}, want ['budget']")
    test = " ".join((svc.get("healthcheck") or {}).get("test") or [])
    if "/mcp" not in test or "node" not in test:
        errors.append(f"{name} healthcheck does not probe /mcp with node")

if "actual_budget" in (cfg.get("secrets") or {}):
    errors.append("the single budget's actual_budget secret is still declared")

# budget is a plain bridge network so the MCP servers can reach a remote
# Actual server. It stays dedicated: exactly these four services join it.
budget = (cfg.get("networks") or {}).get("budget")
if budget is None:
    errors.append("network budget is not declared")
elif budget.get("internal"):
    errors.append("network budget must not be internal (remote Actual servers)")
members = {n for n, svc in services.items() if "budget" in set(svc.get("networks") or {})}
want = {"hermes", "actual-server", "actual-mcp-rw", "actual-mcp-ro"}
if members != want:
    errors.append(f"budget network members are {sorted(members)}, want {sorted(want)}")

for name, svc in services.items():
    keys = {k for k in env_map(svc) if k.startswith("ACTUAL_")}
    if "ACTUAL_PASSWORD" in keys:
        errors.append(f"{name} environment includes ACTUAL_PASSWORD")
for name in ("hermes-api-bootstrap", "hermes-browser-bootstrap"):
    svc = services.get(name) or {}
    env = env_map(svc)
    keys = {k for k in env if k.startswith("ACTUAL_")}
    if keys != {"ACTUAL_MCP_ENABLED"}:
        errors.append(f"{name} ACTUAL_* keys are {sorted(keys)}, want only ACTUAL_MCP_ENABLED")
    # Only the instance files: the bootstrap never sees secrets/.
    vols = svc.get("volumes") or []
    rows = [v for v in vols if v.get("target") == "/bootstrap/addon-data/actual/instances"]
    if len(rows) != 1 or not rows[0].get("read_only") \
            or not str(rows[0].get("source", "")).endswith("/data/addons/actual/instances"):
        errors.append(f"{name} does not mount <data root>/addons/actual/instances read-only ({rows})")
    for v in vols:
        if "addons/actual" in str(v.get("source", "")) and not str(v.get("source", "")).endswith("/instances"):
            errors.append(f"{name} mounts {v.get('source')}; only addons/actual/instances may reach it")
        if "actual_rows" in str(v.get("target", "")):
            errors.append(f"{name} still mounts actual_rows.py")

for err in errors:
    print(f"FAIL {err}", file=sys.stderr)
raise SystemExit(1 if errors else 0)
EOF

printf 'OK Actual Budget compose profiles\n'
