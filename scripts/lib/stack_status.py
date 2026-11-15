"""Builds scripts/status.sh's JSON from `docker compose config` and `ps`.

stdin: the `docker compose config --format json` document, a line holding
only PS_MARKER, then `docker compose ps --all --format json` output (JSON lines
from Compose 2.21 on, one array before). An empty ps section with
--engine-down means the engine didn't answer.

Prints one JSON object (see scripts/status.sh). Reads no secrets: it keeps
only service names, states and port numbers from the config.
"""

from __future__ import annotations

import argparse
import json
import sys

PS_MARKER = "---compose-ps---"

# Compose container states that mean "not serving".
STOPPED = {"exited", "created", "removing", "dead"}


def _ps_rows(text: str) -> list[dict]:
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _port(host_ip, host, container, protocol) -> dict | None:
    try:
        host = int(host)
        container = int(container)
    except (TypeError, ValueError):
        return None
    if host <= 0:
        return None
    return {"host_ip": host_ip or "0.0.0.0", "host": host, "container": container,
            "protocol": protocol or "tcp"}


def _config_ports(svc: dict) -> list[dict]:
    out = []
    for p in svc.get("ports") or []:
        port = _port(p.get("host_ip"), p.get("published"), p.get("target"), p.get("protocol"))
        if port:
            out.append(port)
    return out


def _ps_ports(row: dict) -> list[dict]:
    out, seen = [], set()
    for p in row.get("Publishers") or []:
        port = _port(p.get("URL"), p.get("PublishedPort"), p.get("TargetPort"), p.get("Protocol"))
        if port:
            key = (port["host_ip"], port["host"], port["container"], port["protocol"])
            # Docker lists an IPv4 and an IPv6 publisher for one 0.0.0.0 binding.
            if key not in seen:
                seen.add(key)
                out.append(port)
    return out


def _state(rows: list[dict]) -> str:
    states = {(r.get("State") or "").lower() for r in rows}
    if not states or states <= STOPPED:
        return "stopped"
    for s in ("running", "restarting", "paused"):
        if s in states:
            return s
    return sorted(states)[0]


def build(config: dict, ps: list[dict] | None, version: str, images: dict, host: dict) -> dict:
    by_service: dict[str, list[dict]] = {}
    for row in ps or []:
        by_service.setdefault(row.get("Service") or "", []).append(row)
    services = []
    for name in sorted((config.get("services") or {}).keys()):
        svc = config["services"][name]
        rows = by_service.get(name, [])
        if ps is None:
            state, ports = "unknown", _config_ports(svc)
        else:
            state = _state(rows)
            running = [r for r in rows if (r.get("State") or "").lower() == "running"]
            ports = _ps_ports(running[0]) if running else _config_ports(svc)
        entry = {"name": name, "state": state, "ports": ports}
        health = next((r.get("Health") for r in rows if r.get("Health")), "")
        if health:
            entry["health"] = health
        services.append(entry)
    return {
        "version": version,
        "images": images,
        "project": config.get("name") or "",
        "host": host,
        "engine": ps is not None,
        "services": services,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--hermes-agent", default="")
    ap.add_argument("--hermes-webui", default="")
    ap.add_argument("--os", default="")
    ap.add_argument("--arch", default="")
    ap.add_argument("--engine-down", action="store_true")
    args = ap.parse_args(argv)

    text = sys.stdin.read()
    cfg_text, _, ps_text = text.partition("\n" + PS_MARKER + "\n")
    try:
        config = json.loads(cfg_text) if cfg_text.strip() else {}
    except ValueError:
        print("error: docker compose config did not return JSON", file=sys.stderr)
        return 1
    try:
        ps = None if args.engine_down else _ps_rows(ps_text)
    except ValueError:
        print("error: docker compose ps did not return JSON", file=sys.stderr)
        return 1
    out = build(
        config,
        ps,
        args.version,
        {"hermes_agent": args.hermes_agent, "hermes_webui": args.hermes_webui},
        {"os": args.os, "arch": args.arch},
    )
    json.dump(out, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
