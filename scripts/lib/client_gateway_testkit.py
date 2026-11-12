"""Shared pieces for scripts/test-client-gateway-*.py.

The gateway needs Starlette, uvicorn, websockets and jsonschema, which the
pinned Hermes image has and the host may not. So each test re-runs itself
inside that image (``run_in_image``) with the repo's scripts/ and
compose/client-gateway/ mounted read-only, and does its work when it sees
``--in-image``. ``serve`` starts the gateway in-process on a free port.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "test-token-" + "x" * 32
IN_IMAGE = "--in-image" in sys.argv


def run_in_image(test_file: str) -> None:
    """Host side: run ``test_file --in-image`` inside the pinned Hermes image."""
    sys.path.insert(0, str(ROOT / "scripts" / "lib"))
    import hermes_image

    tag = hermes_image.image_gate()
    if tag is None:
        return
    rel = Path(test_file).resolve().relative_to(ROOT)
    driver = (
        "import runpy, sys\n"
        f"sys.argv = ['/repo/{rel}', '--in-image']\n"
        f"runpy.run_path('/repo/{rel}', run_name='__main__')\n"
        "print('image ok')\n"
    )
    hermes_image.run_driver(
        driver,
        tag=tag,
        mounts=[
            (ROOT / "scripts", "/repo/scripts"),
            (ROOT / "compose" / "client-gateway", "/repo/compose/client-gateway"),
        ],
    )


def in_image_paths() -> None:
    """Make client_gateway and scripts/lib importable inside the image."""
    for p in (ROOT / "compose" / "client-gateway", ROOT / "scripts" / "lib"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    os.environ.setdefault("HOME", "/tmp")


@contextlib.asynccontextmanager
async def serve(backend, **settings_overrides):
    """Run the gateway app on 127.0.0.1:<free port>; yields ``ws://127.0.0.1:<port>``."""
    import warnings

    import uvicorn
    from client_gateway.auth import Settings
    from client_gateway.server import create_app

    warnings.filterwarnings("ignore", message="The `websockets` implementation is deprecated")
    fields = {"token": TOKEN, "validate_frames": True, **settings_overrides}
    app = create_app(Settings(**fields), backend)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", ws="websockets", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


async def client(url: str, path: str = "/acp", token: str | None = TOKEN, origin: str | None = None):
    from acp_client import AcpClient
    from client_gateway.validate import Validator

    return await AcpClient.connect(url + path, token, origin=origin, validator=Validator())


async def upgrade_status(url: str, path: str = "/acp", token: str | None = TOKEN, origin: str | None = None) -> int:
    """The HTTP status a refused upgrade gets, or 101 when it's accepted."""
    import websockets

    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    try:
        ws = await websockets.connect(url + path, additional_headers=headers)
    except websockets.InvalidStatus as exc:
        return exc.response.status_code
    await ws.close()
    return 101
