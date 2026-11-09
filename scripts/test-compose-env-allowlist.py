#!/usr/bin/env python3
"""Assert compose gives each service only its own secret names.

Runs `docker compose config` with the full profile set and compares
environment key names. It does not print secret values.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Every profile that publishes a service we want to see in one config.
PROFILES = "core,rag,neo4j,calendar,automation,research,coding,ollama,searxng-prod,search-gate,db,todo,crm,mail,mail-drafts,messages,bitwarden,bitwarden-tools,budget,budget-local"

# Secret-bearing environment keys, and the services allowed to carry them.
# An empty set means the key must not appear on any service.
SECRET_KEYS = {
    "POSTGRES_PASSWORD": {"postgres", "postgres-db", "twenty-db"},
    "DB_ADMIN_PASSWORD": set(),
    "PGPASSWORD": {"db-provision"},
    "DB_AGENT_RO_PASSWORD": {"db-provision"},
    "DB_AGENT_RW_PASSWORD": {"db-provision"},
    "DATABASE_URI": {"sql-mcp-ro", "sql-mcp-rw"},
    # From compose/postgres/services/vikunja.env (env_file) when it exists.
    "DB_SERVICE_PASSWORD": {"vikunja"},
    "DATABASE_URL": {"vikunja"},
    "VIKUNJA_DATABASE_PASSWORD": {"vikunja"},
    "VIKUNJA_SECRET": set(),
    "VIKUNJA_SERVICE_SECRET": {"vikunja"},
    # The managed account's password; only the /login-local proxy needs it.
    "VIKUNJA_PASSWORD": {"vikunja-proxy"},
    # Bootstrap copies each token into one profile's .env; hermes reads it there.
    "VIKUNJA_MCP_TOKEN_RO": {"hermes-api-bootstrap", "hermes-browser-bootstrap"},
    "VIKUNJA_MCP_TOKEN_RW": {"hermes-api-bootstrap", "hermes-browser-bootstrap"},
    # Twenty (crm profile). The stack-level names never reach a container;
    # Twenty's own names carry them to the server and worker only.
    "TWENTY_ENCRYPTION_KEY": set(),
    "TWENTY_DB_PASSWORD": set(),
    "TWENTY_PASSWORD": set(),
    "ENCRYPTION_KEY": {"twenty-server", "twenty-worker"},
    "PG_DATABASE_URL": {"twenty-server", "twenty-worker"},
    # Actual Budget. The MCP containers read these from compose/actual/budget.env
    # mounted at /app/.env, so no service carries them in its environment.
    "ACTUAL_PASSWORD": set(),
    "ACTUAL_SESSION_TOKEN": set(),
    "ACTUAL_BUDGET_ENCRYPTION_PASSWORD": set(),
    "BEARER_TOKEN": set(),
    "MESSAGES_WEBHOOK_SECRET": {"messages-mcp", "hermes-api-bootstrap", "hermes-browser-bootstrap"},
    # n8n stores its data in SQLite; no service gets n8n Postgres settings.
    "DB_POSTGRESDB_PASSWORD": set(),
    "N8N_ENCRYPTION_KEY": {"n8n", "n8n-import"},
    "N8N_USER_MANAGEMENT_JWT_SECRET": {"n8n", "n8n-import"},
    "NEO4J_PASSWORD": {"lightrag", "n8n-import"},
    "LIGHTRAG_API_KEY": {"lightrag", "n8n-import"},
    "OPENCODE_SERVER_PASSWORD": {"opencode", "opencode-mcp"},
    "HERMES_DASHBOARD_PASSWORD": set(),
    "HERMES_DASHBOARD_AUTH_SECRET": set(),
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": {"hermes"},
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET": {"hermes"},
    "HERMES_DASHBOARD_HANDOFF_SECRET": {"hermes"},
    "SEARXNG_SECRET": set(),
    "SEARCH_GATE_FALLBACK_AUTH": {"search-gate"},
    "BUZZ_OPERATOR_PRIVATE_KEY": set(),
    "API_SERVER_KEY": {"hermes-webui"},
    "LLM_BINDING_API_KEY": {"llm-proxy", "lightrag"},
    "LIGHTRAG_LLM_BINDING_API_KEY": {"llm-proxy"},
    "EMBEDDING_BINDING_API_KEY": {"llm-proxy", "lightrag"},
    "OPENAI_API_KEY": {"gpt-researcher", "gptr-mcp"},
    "LLM_PROXY_ADMIN_TOKEN": {"llm-proxy"},
    "SIGNAL_ACCOUNT": set(),
    "SIGNAL_ALLOWED_USERS": set(),
    "SIGNAL_HOME_CHANNEL": set(),
    "SIGNAL_HTTP_URL": set(),
    # bw reads these from its environment; the broker hands them to bw only
    # in the child process it starts, so no container carries them.
    "BW_CLIENTID": set(),
    "BW_CLIENTSECRET": set(),
    "BW_PASSWORD": set(),
    "BW_SESSION": set(),
}

# hermes-worker may carry only these. Any other key, including a secret, fails.
WORKER_ENV = {"HERMES_UID", "HERMES_GID", "TZ"}

# bitwarden-broker shares hermes' network namespace and may carry only these.
BROKER_ENV = {
    "BITWARDEN_ACCOUNTS_DIR",
    "BITWARDEN_DATA_DIR",
    "BITWARDEN_SECRETS_DIR",
    "BITWARDEN_SYNC_MINUTES",
    "BITWARDEN_IDLE_LOCK_MINUTES",
    "BITWARDEN_LISTEN",
    "BITWARDEN_SYNC_SOCKET",
    "NODE_EXTRA_CA_CERTS",
}
# Actual Budget services may carry only these keys. The MCP containers get
# their credentials from the /app/.env file, never from the environment.
ACTUAL_ENV = {
    "actual-server": set(),
    "actual-mcp-rw": {"ACTUAL_SYNC_TTL_MS", "ACTUAL_DATA_DIR"},
    "actual-mcp-ro": {"ACTUAL_SYNC_TTL_MS", "ACTUAL_DATA_DIR"},
}
# The only ACTUAL_* keys any other service may carry.
ACTUAL_BOOTSTRAP_KEYS = {"ACTUAL_MCP_ENABLED", "ACTUAL_ACCESS"}
ACTUAL_BOOTSTRAPS = {"hermes-api-bootstrap", "hermes-browser-bootstrap"}

# Keys that must still be present after the allowlist, so a drop is caught.
REQUIRED = {
    "lightrag": {"NEO4J_PASSWORD", "LIGHTRAG_API_KEY"},
    "n8n": {"N8N_ENCRYPTION_KEY", "N8N_USER_MANAGEMENT_JWT_SECRET"},
    "n8n-import": {
        "N8N_ENCRYPTION_KEY",
        "N8N_USER_MANAGEMENT_JWT_SECRET",
        "LIGHTRAG_API_KEY",
        "NEO4J_PASSWORD",
    },
    "hermes": {
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
        "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
        "HERMES_BUZZ_ENABLED",
        "SEARXNG_URL",
    },
    "opencode": {"OPENCODE_SERVER_PASSWORD"},
    "opencode-mcp": {"OPENCODE_SERVER_PASSWORD"},
    "gpt-researcher": {"OPENAI_API_KEY"},
    "gptr-mcp": {"OPENAI_API_KEY"},
    "llm-proxy": {
        "LLM_BINDING_API_KEY",
        "LIGHTRAG_LLM_BINDING_API_KEY",
        "EMBEDDING_BINDING_API_KEY",
        "LLM_PROXY_ADMIN_TOKEN",
    },
    "postgres-db": {"POSTGRES_PASSWORD"},
    "db-provision": {"PGPASSWORD", "DB_AGENT_RO_PASSWORD", "DB_AGENT_RW_PASSWORD"},
    "sql-mcp-ro": {"DATABASE_URI"},
    "sql-mcp-rw": {"DATABASE_URI"},
    "hermes-webui": {"API_SERVER_KEY"},
    "vikunja": {"VIKUNJA_SERVICE_SECRET"},
    "vikunja-proxy": {"VIKUNJA_PASSWORD"},
    "twenty-server": {"ENCRYPTION_KEY", "PG_DATABASE_URL"},
    "twenty-worker": {"ENCRYPTION_KEY", "PG_DATABASE_URL"},
    "twenty-db": {"POSTGRES_PASSWORD"},
    "actual-mcp-rw": {"ACTUAL_SYNC_TTL_MS", "ACTUAL_DATA_DIR"},
    "actual-mcp-ro": {"ACTUAL_SYNC_TTL_MS", "ACTUAL_DATA_DIR"},
    "hermes-api-bootstrap": {"ACTUAL_MCP_ENABLED", "ACTUAL_ACCESS"},
    "hermes-browser-bootstrap": {"ACTUAL_MCP_ENABLED", "ACTUAL_ACCESS"},
}

# Clients may carry these names only when the value is the placeholder.
# The real keys are interpolated on llm-proxy.
PLACEHOLDER = "local-llm"
PLACEHOLDER_ON = {
    "lightrag": {"LLM_BINDING_API_KEY", "EMBEDDING_BINDING_API_KEY"},
    "gpt-researcher": {"OPENAI_API_KEY"},
    "gptr-mcp": {"OPENAI_API_KEY"},
}


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)


def env_map(service: dict) -> dict:
    raw = service.get("environment") or {}
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    mapped = {}
    for item in raw:
        if not isinstance(item, str):
            continue
        if "=" in item:
            key, value = item.split("=", 1)
            mapped[key] = value
        else:
            mapped[item] = None
    return mapped


def load_config() -> dict:
    env = os.environ.copy()
    env["COMPOSE_PROFILES"] = PROFILES
    # A local .env must not hide the worker allowlist. ssh is the default layout.
    env["HERMES_TERMINAL_BACKEND"] = "ssh"
    proc = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "docker compose config failed").strip()
        fail(err)
        raise SystemExit(1)
    return json.loads(proc.stdout)


def check_secrets(services: dict) -> list[str]:
    errors = []
    for name, service in services.items():
        keys = set(env_map(service))
        for key in sorted(keys & SECRET_KEYS.keys()):
            allowed = SECRET_KEYS[key]
            if name not in allowed:
                errors.append(f"{name} environment includes {key}")
        missing = REQUIRED.get(name, set()) - keys
        for key in sorted(missing):
            errors.append(f"{name} environment is missing {key}")
    for name in REQUIRED:
        if name not in services:
            errors.append(f"service {name} missing from compose config")
    hermes_env = env_map(services.get("hermes") or {})
    if "SEARXNG_URL" in hermes_env and hermes_env["SEARXNG_URL"] not in ("", None):
        errors.append("hermes SEARXNG_URL must be empty")
    return errors


def check_actual(services: dict) -> list[str]:
    errors = []
    for name, allowed in ACTUAL_ENV.items():
        service = services.get(name)
        if not service:
            errors.append(f"service {name} missing from compose config")
            continue
        for key in sorted(set(env_map(service)) - allowed):
            errors.append(f"{name} environment includes {key}")
        if service.get("env_file"):
            errors.append(f"{name} has an env_file; credentials go through /app/.env")
    for name, service in services.items():
        if name in ACTUAL_ENV:
            continue
        actual_keys = {k for k in env_map(service) if k.startswith("ACTUAL_")}
        allowed = ACTUAL_BOOTSTRAP_KEYS if name in ACTUAL_BOOTSTRAPS else set()
        for key in sorted(actual_keys - allowed):
            errors.append(f"{name} environment includes {key}")
    return errors


def check_proxy(services: dict) -> list[str]:
    errors = []
    service = services.get("llm-proxy")
    if not service:
        return ["service llm-proxy missing from compose config"]
    env = env_map(service)
    for name, keys in PLACEHOLDER_ON.items():
        client = env_map(services.get(name) or {})
        for key in sorted(keys):
            if key in client and client[key] != PLACEHOLDER:
                errors.append(f"{name} {key} must be the placeholder {PLACEHOLDER}")
    published = []
    for port in service.get("ports") or []:
        if not isinstance(port, dict):
            errors.append("llm-proxy has an unparsed published port")
            continue
        try:
            target = int(port.get("target"))
        except (TypeError, ValueError):
            errors.append("llm-proxy has an unparsed published port")
            continue
        published.append(target)
        if target == 4000:
            errors.append("llm-proxy publishes the inference port")
    if 4001 not in published:
        errors.append("llm-proxy admin port is not published")
    if "LLM_PROXY_ADMIN_TOKEN" in env and not env["LLM_PROXY_ADMIN_TOKEN"]:
        errors.append("llm-proxy admin token is empty")
    return errors


def check_worker(services: dict) -> list[str]:
    errors = []
    service = services.get("hermes-worker")
    if not service:
        return ["service hermes-worker missing from compose config"]
    keys = set(env_map(service))
    for key in sorted(keys - WORKER_ENV):
        errors.append(f"hermes-worker environment includes {key}")
    for key in sorted(SECRET_KEYS):
        if key in keys:
            errors.append(f"hermes-worker environment includes secret {key}")
    if service.get("ports"):
        errors.append("hermes-worker publishes a host port")
    return errors


def check_broker(services: dict) -> list[str]:
    service = services.get("bitwarden-broker")
    if not service:
        return ["service bitwarden-broker missing from compose config"]
    keys = set(env_map(service))
    errors = [f"bitwarden-broker environment includes {key}" for key in sorted(keys - BROKER_ENV)]
    errors += [f"bitwarden-broker environment is missing {key}" for key in sorted(BROKER_ENV - keys)]
    if service.get("env_file"):
        errors.append("bitwarden-broker reads an env_file")
    return errors


def check_loopback(services: dict) -> list[str]:
    errors = []
    for name, service in services.items():
        for port in service.get("ports") or []:
            if not isinstance(port, dict):
                errors.append(f"{name} has an unparsed published port")
                continue
            host_ip = port.get("host_ip")
            target = port.get("target")
            if host_ip != "127.0.0.1":
                errors.append(
                    f"{name} publishes {target} without a 127.0.0.1 bind"
                )
    return errors


# The shared Postgres and its SQL MCP sidecars live on the internal db network
# only, publish nothing, and hermes is the sole bridge from the default network.
DB_ONLY = {"postgres-db", "db-provision", "sql-mcp-ro", "sql-mcp-rw"}
# Services on both default and db. An integration that stores data and needs
# the internet goes here; one that needs neither goes in DB_ONLY.
DB_BRIDGES = {"hermes", "vikunja"}
# Networks a DB_BRIDGES service joins besides default and db. hermes is on
# vault, the internal network the Bitwarden broker reaches the local Vaultwarden
# through, and on budget for the Actual Budget MCPs.
BRIDGE_EXTRA_NETS = {"hermes": {"budget", "vault"}}


def check_db_network(services: dict, networks: dict) -> list[str]:
    errors = []
    db_net = next((net for key, net in networks.items() if key == "db"), None)
    if not db_net:
        return ["network db missing from compose config"]
    if not db_net.get("internal"):
        errors.append("network db must be internal")
    for name in sorted(DB_ONLY):
        service = services.get(name)
        if not service:
            errors.append(f"service {name} missing from compose config")
            continue
        if service.get("ports"):
            errors.append(f"{name} publishes a host port")
        nets = set(service.get("networks") or {})
        if nets != {"db"}:
            errors.append(f"{name} networks are {sorted(nets)}, want ['db']")
    for name in sorted(DB_BRIDGES):
        nets = set((services.get(name) or {}).get("networks") or {})
        want = {"default", "db"} | BRIDGE_EXTRA_NETS.get(name, set())
        if nets != want:
            errors.append(f"{name} networks are {sorted(nets)}, want {sorted(want)}")
    for name, service in services.items():
        if name in DB_ONLY or name in DB_BRIDGES:
            continue
        if "db" in set(service.get("networks") or {}):
            errors.append(f"{name} joins the db network; add it to DB_ONLY or DB_BRIDGES on purpose")
    return errors


# n8n and n8n-import must read one SQLite store, or the import's "database
# exists" check looks somewhere n8n never reads.
N8N_HOME = "/home/node/.n8n"

# Keys from the x-n8n anchor that both services must keep. A service-level
# environment list replaces the anchor's, which once sent n8n's diagnostics
# to n8n and dropped the workflow URLs.
N8N_SHARED_ENV = {
    "N8N_DIAGNOSTICS_ENABLED": "false",
    "N8N_PERSONALIZATION_ENABLED": "false",
    "LIGHTRAG_URL": "http://lightrag:9621",
    "NEO4J_HTTP_URL": "http://neo4j:7474",
    "SEARXNG_URL": "http://searxng:8080",
}


def n8n_home_source(service: dict) -> str | None:
    for vol in service.get("volumes") or []:
        if isinstance(vol, dict) and vol.get("target") == N8N_HOME:
            return vol.get("source")
    return None


def check_n8n_store(services: dict) -> list[str]:
    errors = []
    sources = {}
    for name in ("n8n", "n8n-import"):
        service = services.get(name)
        if not service:
            errors.append(f"service {name} missing from compose config")
            continue
        env = env_map(service)
        if env.get("DB_TYPE") != "sqlite":
            errors.append(f"{name} DB_TYPE is {env.get('DB_TYPE')!r}, want 'sqlite'")
        for key in sorted(k for k in env if k.startswith("DB_POSTGRESDB_")):
            errors.append(f"{name} environment includes {key}")
        for key, want in sorted(N8N_SHARED_ENV.items()):
            if env.get(key) != want:
                errors.append(f"{name} {key} is {env.get(key)!r}, want {want!r}")
        sources[name] = n8n_home_source(service)
        if not sources[name]:
            errors.append(f"{name} does not mount {N8N_HOME}")
    if len(set(sources.values())) > 1:
        errors.append(f"n8n and n8n-import mount different {N8N_HOME}: {sources}")
    if "postgres" in services:
        errors.append("deprecated n8n postgres service starts with the default profiles")
    return errors


def main() -> None:
    config = load_config()
    services = config.get("services") or {}
    errors = check_secrets(services) + check_worker(services) + check_proxy(services) + check_loopback(services)
    errors += check_broker(services)
    errors += check_db_network(services, config.get("networks") or {})
    errors += check_n8n_store(services)
    errors += check_actual(services)
    if errors:
        for err in errors:
            fail(err)
        raise SystemExit(1)
    print(
        f"OK compose env allowlists ({len(services)} services, profiles={PROFILES})"
    )


if __name__ == "__main__":
    main()
