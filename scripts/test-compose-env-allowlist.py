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
PROFILES = "core,rag,calendar,automation,coding,ollama,searxng-prod"

# Secret-bearing environment keys, and the services allowed to carry them.
# An empty set means the key must not appear on any service.
SECRET_KEYS = {
    "POSTGRES_PASSWORD": {"postgres"},
    "DB_POSTGRESDB_PASSWORD": {"n8n-import"},
    "N8N_ENCRYPTION_KEY": {"n8n", "n8n-import"},
    "N8N_USER_MANAGEMENT_JWT_SECRET": {"n8n", "n8n-import"},
    "NEO4J_PASSWORD": {"lightrag"},
    "LIGHTRAG_API_KEY": {"lightrag"},
    "OPENCODE_SERVER_PASSWORD": {"opencode", "opencode-mcp"},
    "HERMES_DASHBOARD_PASSWORD": set(),
    "HERMES_DASHBOARD_AUTH_SECRET": set(),
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": {"hermes"},
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET": {"hermes"},
    "SEARXNG_SECRET": set(),
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
}

# hermes-worker may carry only these. Any other key, including a secret, fails.
WORKER_ENV = {"HERMES_UID", "HERMES_GID", "TZ"}

# Keys that must still be present after the allowlist, so a drop is caught.
REQUIRED = {
    "lightrag": {"NEO4J_PASSWORD", "LIGHTRAG_API_KEY"},
    "n8n": {"N8N_ENCRYPTION_KEY", "N8N_USER_MANAGEMENT_JWT_SECRET", "GPTR_URL"},
    "n8n-import": {
        "N8N_ENCRYPTION_KEY",
        "N8N_USER_MANAGEMENT_JWT_SECRET",
        "DB_POSTGRESDB_PASSWORD",
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
    "postgres": {"POSTGRES_PASSWORD"},
    "hermes-webui": {"API_SERVER_KEY"},
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


def main() -> None:
    config = load_config()
    services = config.get("services") or {}
    errors = check_secrets(services) + check_worker(services) + check_proxy(services) + check_loopback(services)
    if errors:
        for err in errors:
            fail(err)
        raise SystemExit(1)
    print(
        f"OK compose env allowlists ({len(services)} services, profiles={PROFILES})"
    )


if __name__ == "__main__":
    main()
