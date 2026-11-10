"""`docker compose config` rendered as JSON, for tests that check the stack's wiring.

Only docker-compose.yml is loaded, and ASSISTANT_DATA_ROOT is ./data unless
the caller passes another. A local docker-compose.override.yml or a data root
set in .env would make the same test pass in CI and fail on a workstation (or
the reverse). Renders are cached per process,
so tests that ask for the same profiles and environment twice run compose once.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def render(profiles: str, env: dict[str, str] | None = None, drop: tuple[str, ...] = ()) -> dict:
    """The rendered config for COMPOSE_PROFILES=profiles.

    ``env`` sets variables on top of the caller's environment and ``drop``
    removes them first. On failure it prints FAIL with compose's last error
    line and exits 1. Callers must not mutate the result: it is shared.
    """
    return _render(profiles, tuple(sorted((env or {}).items())), tuple(sorted(drop)))


@lru_cache(maxsize=None)
def _render(profiles: str, env_items: tuple[tuple[str, str], ...], drop: tuple[str, ...]) -> dict:
    env = os.environ.copy()
    for key in drop:
        env.pop(key, None)
    # Set in the environment, it wins over .env.
    env["ASSISTANT_DATA_ROOT"] = "./data"
    env.update(env_items)
    env["COMPOSE_PROFILES"] = profiles
    proc = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "config", "--format", "json"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "docker compose config failed").strip().splitlines()
        label = ", ".join([profiles, *(f"{k}={v}" for k, v in env_items)])
        print(f"FAIL docker compose config ({label}): {err[-1] if err else ''}", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(proc.stdout)
