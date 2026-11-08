#!/bin/sh
# Shape of the budget and budget-local profiles (Actual Budget) in
# docker-compose.yml, the flags that turn them on, and the files that keep the
# budget credentials out of git and out of container environments.
# Needs a .env that satisfies compose interpolation, as CI writes. Prints no
# secret values.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

grep -q '^ACTUAL_LOCAL_ENABLED budget-local 0$' scripts/sync-profiles.sh \
  || fail "sync-profiles.sh maps ACTUAL_LOCAL_ENABLED to budget-local"

for k in ACTUAL_MCP_ENABLED ACTUAL_LOCAL_ENABLED ACTUAL_ACCESS ACTUAL_PORT ACTUAL_DATA_DIR ACTUAL_SERVER_IMAGE ACTUAL_BUDGET_FILE; do
  grep -qE "^#? ?${k}=" .env.example || fail ".env.example documents ${k}"
done
grep -qE '^ACTUAL_MCP_ENABLED=0$' .env.example || fail ".env.example has ACTUAL_MCP_ENABLED=0"
grep -qE '^ACTUAL_LOCAL_ENABLED=0$' .env.example || fail ".env.example has ACTUAL_LOCAL_ENABLED=0"

# The budget file: the MCP server's own key names, no values.
example=compose/actual/budget.env.example
[ -f "$example" ] || fail "${example} exists"
for k in ACTUAL_SERVER_URL ACTUAL_WEB_URL ACTUAL_PASSWORD ACTUAL_BUDGET_SYNC_ID ACTUAL_BUDGET_ENCRYPTION_PASSWORD; do
  grep -qE "^${k}=" "$example" || fail "${example} has ${k}"
done
for k in ACTUAL_PASSWORD ACTUAL_BUDGET_SYNC_ID ACTUAL_BUDGET_ENCRYPTION_PASSWORD; do
  grep -qE "^${k}=$" "$example" || fail "${example} leaves ${k} empty"
done

# The placeholder the secret falls back to is tracked and empty.
[ -f compose/actual/no-budget.env ] || fail "compose/actual/no-budget.env exists"
[ ! -s compose/actual/no-budget.env ] || fail "compose/actual/no-budget.env is empty"
for f in compose/actual/budget.env.example compose/actual/no-budget.env; do
  if git check-ignore -q "$f"; then fail "$f must stay tracked"; fi
done
for f in compose/actual/budget.env compose/actual/.budget.env.tmp compose/actual/other; do
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

# With no profiles, no Actual service is defined.
env -u ACTUAL_BUDGET_FILE COMPOSE_PROFILES= docker compose config --format json > "${tmp}/none.json" \
  || fail "docker compose config with no profiles"
env -u ACTUAL_BUDGET_FILE COMPOSE_PROFILES=core,budget,budget-local docker compose config --format json > "${tmp}/budget.json" \
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
if set(server.get("networks") or {}) != {"default", "budget"}:
    errors.append(f"actual-server networks are {sorted(server.get('networks') or {})}, want ['budget', 'default']")

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
    secrets = svc.get("secrets") or []
    if not any(s.get("source") == "actual_budget" and s.get("target") == "/app/.env" for s in secrets):
        errors.append(f"{name} does not get secret actual_budget at /app/.env")
    cache = name.replace("-", "_") + "_cache"
    vols = svc.get("volumes") or []
    if not any(v.get("type") == "volume" and v.get("source") == cache and v.get("target") == "/data" for v in vols):
        errors.append(f"{name} does not mount volume {cache} at /data")
    if set(svc.get("networks") or {}) != {"budget"}:
        errors.append(f"{name} networks are {sorted(svc.get('networks') or {})}, want ['budget']")
    test = " ".join((svc.get("healthcheck") or {}).get("test") or [])
    if "/mcp" not in test or "node" not in test:
        errors.append(f"{name} healthcheck does not probe /mcp with node")

secret = (cfg.get("secrets") or {}).get("actual_budget") or {}
if not str(secret.get("file", "")).endswith("compose/actual/no-budget.env"):
    errors.append(f"secret actual_budget falls back to {secret.get('file')}, want compose/actual/no-budget.env")

budget = (cfg.get("networks") or {}).get("budget")
if not budget:
    errors.append("network budget is not declared")
elif not budget.get("internal"):
    errors.append("network budget must be internal")
if "budget" not in set(services.get("hermes", {}).get("networks") or {}):
    errors.append("hermes is not on the budget network")
for name, svc in services.items():
    if name in ACTUAL or name == "hermes":
        continue
    if "budget" in set(svc.get("networks") or {}):
        errors.append(f"{name} joins the budget network")

for name, svc in services.items():
    keys = {k for k in env_map(svc) if k.startswith("ACTUAL_")}
    if "ACTUAL_PASSWORD" in keys:
        errors.append(f"{name} environment includes ACTUAL_PASSWORD")
for name in ("hermes-api-bootstrap", "hermes-browser-bootstrap"):
    keys = {k for k in env_map(services.get(name) or {}) if k.startswith("ACTUAL_")}
    if keys != {"ACTUAL_MCP_ENABLED", "ACTUAL_ACCESS"}:
        errors.append(f"{name} ACTUAL_* keys are {sorted(keys)}, want ACTUAL_ACCESS and ACTUAL_MCP_ENABLED")

for err in errors:
    print(f"FAIL {err}", file=sys.stderr)
raise SystemExit(1 if errors else 0)
EOF

printf 'OK Actual Budget compose profiles\n'
