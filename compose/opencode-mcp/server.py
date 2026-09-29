"""
OpenCode MCP bridge for the assistant stack.

Exposes a small set of coding_* tools so Hermes can delegate implement/refactor
work to OpenCode's HTTP API without opening the OpenCode UI.
"""

from __future__ import annotations

import logging
import os
import json
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s] - %(message)s",
)
logger = logging.getLogger("opencode-mcp")

OPENCODE_BASE_URL = os.environ.get("OPENCODE_BASE_URL", "http://opencode:4096").rstrip("/")
OPENCODE_USER = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
OPENCODE_PASS = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
PRIMARY_ROOT = os.environ.get("OPENCODE_WORKSPACE_HOST", "").rstrip("/")
EXTRA_ROOTS_RAW = os.environ.get("CODING_EXTRA_ROOTS", "")
TASK_DEFAULT_TIMEOUT = int(os.environ.get("TASK_DEFAULT_TIMEOUT_SEC", "600"))
POLL_INTERVAL_SEC = float(os.environ.get("TASK_POLL_INTERVAL_SEC", "2.0"))

mcp = FastMCP(name="OpenCode Coding")

# task_id -> task state
_tasks: Dict[str, Dict[str, Any]] = {}
_tasks_lock = threading.Lock()


def _coding_roots() -> List[str]:
    roots: List[str] = []
    if PRIMARY_ROOT:
        roots.append(PRIMARY_ROOT)
    for part in EXTRA_ROOTS_RAW.split(":"):
        part = part.strip().rstrip("/")
        if part and part not in roots:
            roots.append(part)
    return roots


def _normalize_directory(directory: str) -> str:
    return directory.rstrip("/") if directory else directory


def _path_allowed(directory: str) -> bool:
    directory = _normalize_directory(directory)
    if not directory or not directory.startswith("/"):
        return False
    for root in _coding_roots():
        if directory == root or directory.startswith(root + "/"):
            return True
    return False


def _needs_access_payload(directory: str) -> Dict[str, Any]:
    directory = _normalize_directory(directory)
    return {
        "ok": False,
        "needs_access": True,
        "path": directory,
        "roots": _coding_roots(),
        "hint": (
            f"Path {directory!r} is outside the coding roots. Ask the user to grant "
            f"access, then have them run: make coding-root-add DIR={directory} "
            f"(or the nearest parent they want to share), recreate hermes/opencode/"
            f"opencode-mcp, and retry."
        ),
    }


def _client() -> httpx.Client:
    auth = (OPENCODE_USER, OPENCODE_PASS) if OPENCODE_PASS else None
    # Coding jobs often run for minutes; keep connect short, read long.
    read_timeout = float(max(TASK_DEFAULT_TIMEOUT, 120))
    return httpx.Client(
        base_url=OPENCODE_BASE_URL,
        auth=auth,
        timeout=httpx.Timeout(read_timeout, connect=15.0, write=60.0, pool=30.0),
    )


def _extract_text(parts: Any) -> str:
    if not parts:
        return ""
    chunks: List[str] = []
    if isinstance(parts, dict):
        parts = parts.get("parts") or []
    if not isinstance(parts, list):
        return str(parts)
    for part in parts:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text" and part.get("text"):
            chunks.append(str(part["text"]))
        elif ptype == "tool" and part.get("state"):
            state = part["state"]
            if isinstance(state, dict) and state.get("status") == "completed":
                tool = part.get("tool") or "tool"
                out = state.get("output")
                if out:
                    chunks.append(f"[{tool}] {out}")
    return "\n".join(chunks).strip()


def _session_busy(client: httpx.Client, session_id: str) -> Optional[bool]:
    """Return True if busy, False if idle, None if unknown/empty."""
    try:
        resp = client.get("/session/status")
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict) or not data:
            return None
        st = data.get(session_id)
        if st is None:
            return None
        if isinstance(st, dict):
            raw = str(st.get("type") or st.get("status") or st.get("state") or "").lower()
        else:
            raw = str(st).lower()
        if raw in ("busy", "running", "pending", "active", "working"):
            return True
        if raw in ("idle", "complete", "completed", "done"):
            return False
    except Exception as exc:  # noqa: BLE001
        logger.debug("session status probe failed: %s", exc)
    return None


def _list_messages(client: httpx.Client, session_id: str, directory: str) -> List[Dict[str, Any]]:
    try:
        resp = client.get(
            f"/session/{session_id}/message",
            params={"directory": directory, "limit": 50},
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("message list failed: %s", exc)
        return []


def _assistant_snapshot(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return id/finish/text for the latest assistant message, if any."""
    for item in reversed(messages):
        if not isinstance(item, dict):
            continue
        info = item.get("info") if isinstance(item.get("info"), dict) else {}
        role = str(info.get("role") or item.get("role") or "").lower()
        if role not in ("assistant", "model"):
            continue
        return {
            "id": info.get("id"),
            "finish": info.get("finish"),
            "completed": (info.get("time") or {}).get("completed")
            if isinstance(info.get("time"), dict)
            else None,
            "text": _extract_text(item),
        }
    return {"id": None, "finish": None, "completed": None, "text": ""}


def _prompt_finished(before_id: Optional[str], snap: Dict[str, Any]) -> bool:
    """True when a new assistant turn finished with a terminal finish reason."""
    if not snap.get("id") or snap["id"] == before_id:
        return False
    finish = str(snap.get("finish") or "").lower()
    # OpenCode uses finish=stop when the agent is done; tool-calls means more turns.
    if finish in ("stop", "end_turn", "length", "error", "cancelled", "aborted"):
        return True
    # Some builds only set time.completed on the final message without finish=stop
    # after tool loops; require finish present and not tool-calls.
    if snap.get("completed") and finish and finish not in ("tool-calls", "tool_calls"):
        return True
    return False


def _latest_assistant_text(client: httpx.Client, session_id: str, directory: str) -> str:
    return _assistant_snapshot(_list_messages(client, session_id, directory)).get("text") or ""


def _run_prompt(
    client: httpx.Client,
    session_id: str,
    prompt: str,
    directory: str,
    task_id: str,
) -> Dict[str, Any]:
    """
    Prefer async prompt + poll messages for a terminal assistant finish.
    Fall back to synchronous /message if prompt_async is unavailable.
    """
    body: Dict[str, Any] = {
        "parts": [{"type": "text", "text": prompt}],
    }
    params = {"directory": directory}

    before = _assistant_snapshot(_list_messages(client, session_id, directory))
    before_id = before.get("id")

    async_resp = client.post(
        f"/session/{session_id}/prompt_async",
        params=params,
        json=body,
    )
    if async_resp.status_code in (200, 204):
        deadline = time.time() + TASK_DEFAULT_TIMEOUT
        poll_n = 0
        while time.time() < deadline:
            busy = _session_busy(client, session_id)
            messages = _list_messages(client, session_id, directory)
            snap = _assistant_snapshot(messages)
            with _tasks_lock:
                if task_id in _tasks:
                    progress = snap.get("text") or ""
                    if busy is True:
                        progress = progress or "OpenCode session busy…"
                    _tasks[task_id]["progress"] = (progress or "")[:500]

            finished = _prompt_finished(before_id, snap)
            poll_n += 1
            if finished:
                return {"raw": None, "text": snap.get("text") or ""}

            # If status API reports idle and we already have a newer assistant
            # message (even mid tool-calls), keep waiting for finish=stop.
            if busy is False and snap.get("id") and snap.get("id") != before_id:
                finish = str(snap.get("finish") or "").lower()
                if finish in ("stop", "end_turn"):
                    return {"raw": None, "text": snap.get("text") or ""}

            time.sleep(POLL_INTERVAL_SEC)

        # Timed out: return whatever we have
        text = _latest_assistant_text(client, session_id, directory)
        if not text:
            raise TimeoutError(
                f"OpenCode task timed out after {TASK_DEFAULT_TIMEOUT}s without a final reply"
            )
        return {"raw": None, "text": text}

    logger.info(
        "prompt_async unavailable (%s); falling back to /message",
        async_resp.status_code,
    )
    resp = client.post(
        f"/session/{session_id}/message",
        params=params,
        json=body,
    )
    resp.raise_for_status()
    data = resp.json()
    text = _extract_text(data)
    if not text and isinstance(data, dict):
        text = _extract_text(data.get("parts")) or str(data)[:2000]
    return {"raw": data, "text": text}


def _get_diff(client: httpx.Client, session_id: str, directory: str) -> Any:
    try:
        resp = client.get(
            f"/session/{session_id}/diff",
            params={"directory": directory},
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("diff fetch failed: %s", exc)
    return None


def _worker(task_id: str) -> None:
    with _tasks_lock:
        task = _tasks[task_id]
        task["status"] = "running"
        directory = task["directory"]
        prompt = task["prompt"]
        session_id = task.get("session_id")

    try:
        with _client() as client:
            if not session_id:
                create = client.post(
                    "/session",
                    params={"directory": directory},
                    json={"title": f"hermes-delegate-{task_id[:8]}"},
                )
                create.raise_for_status()
                sess = create.json()
                session_id = sess.get("id") or sess.get("sessionID") or sess.get("session_id")
                if not session_id:
                    raise RuntimeError(f"OpenCode session create returned no id: {sess}")
                with _tasks_lock:
                    _tasks[task_id]["session_id"] = session_id

            result = _run_prompt(client, session_id, prompt, directory, task_id)
            diff = _get_diff(client, session_id, directory)
            with _tasks_lock:
                _tasks[task_id]["status"] = "completed"
                _tasks[task_id]["result_text"] = result.get("text") or ""
                _tasks[task_id]["diff"] = diff
                _tasks[task_id]["finished_at"] = time.time()
                _tasks[task_id]["progress"] = (result.get("text") or "")[:500]
    except Exception as exc:  # noqa: BLE001
        logger.exception("task %s failed", task_id)
        with _tasks_lock:
            _tasks[task_id]["status"] = "failed"
            _tasks[task_id]["error"] = str(exc)
            _tasks[task_id]["finished_at"] = time.time()


def _start_worker(task_id: str) -> None:
    thread = threading.Thread(target=_worker, args=(task_id,), daemon=True)
    thread.start()


@mcp.tool()
def coding_list_roots() -> Dict[str, Any]:
    """List absolute filesystem roots where coding tasks are allowed."""
    out = {"ok": True, "roots": _coding_roots()}
    return out


@mcp.tool()
def coding_start_task(directory: str, prompt: str) -> Dict[str, Any]:
    """
    Start a coding task in OpenCode for the given directory.

    directory must be an absolute path under a coding root. Returns a task_id
    to poll with coding_wait_for_task / coding_get_task_status.
    If the path is outside coding roots, returns needs_access=true instead.
    """
    directory = _normalize_directory(directory)
    if not prompt or not prompt.strip():
        return {"ok": False, "error": "prompt is required"}
    if not _path_allowed(directory):
        payload = _needs_access_payload(directory)
        return payload

    task_id = str(uuid.uuid4())
    with _tasks_lock:
        _tasks[task_id] = {
            "status": "pending",
            "directory": directory,
            "prompt": prompt.strip(),
            "session_id": None,
            "result_text": None,
            "diff": None,
            "error": None,
            "progress": None,
            "started_at": time.time(),
            "finished_at": None,
        }
    _start_worker(task_id)
    out = {
        "ok": True,
        "task_id": task_id,
        "directory": directory,
        "status": "pending",
        "hint": "Poll coding_get_task_status or coding_wait_for_task, then coding_get_task_result.",
    }
    return out


@mcp.tool()
def coding_get_task_status(
    task_id: str,
    include_progress: bool = True,
) -> Dict[str, Any]:
    """Return status of a coding task: pending, running, completed, or failed."""
    with _tasks_lock:
        task = _tasks.get(task_id)
        if not task:
            return {"ok": False, "error": f"unknown task_id: {task_id}"}
        out: Dict[str, Any] = {
            "ok": True,
            "task_id": task_id,
            "status": task["status"],
            "directory": task["directory"],
            "session_id": task.get("session_id"),
        }
        if task.get("error"):
            out["error"] = task["error"]
        if include_progress and task.get("progress"):
            out["progress"] = task["progress"]
        return out


@mcp.tool()
def coding_wait_for_task(
    task_id: str,
    timeout_sec: Optional[int] = None,
    include_progress: bool = True,
) -> Dict[str, Any]:
    """Block until a coding task finishes or timeout_sec elapses."""
    timeout = timeout_sec if timeout_sec is not None else TASK_DEFAULT_TIMEOUT
    deadline = time.time() + max(1, int(timeout))
    while time.time() < deadline:
        with _tasks_lock:
            task = _tasks.get(task_id)
            if not task:
                return {"ok": False, "error": f"unknown task_id: {task_id}"}
            status = task["status"]
            if status in ("completed", "failed"):
                out = coding_get_task_status(task_id, include_progress=include_progress)
                return out
        time.sleep(POLL_INTERVAL_SEC)
    return {
        "ok": True,
        "task_id": task_id,
        "status": "running",
        "timed_out": True,
        "hint": "Task still running; call coding_wait_for_task again or coding_get_task_status.",
    }


@mcp.tool()
def coding_get_task_result(task_id: str) -> Dict[str, Any]:
    """Fetch the final assistant summary and session diff for a completed task."""
    with _tasks_lock:
        task = _tasks.get(task_id)
        if not task:
            return {"ok": False, "error": f"unknown task_id: {task_id}"}
        if task["status"] == "failed":
            out = {
                "ok": False,
                "task_id": task_id,
                "status": "failed",
                "error": task.get("error"),
            }
            return out
        if task["status"] != "completed":
            return {
                "ok": False,
                "task_id": task_id,
                "status": task["status"],
                "error": "task not completed yet",
            }
        out = {
            "ok": True,
            "task_id": task_id,
            "status": "completed",
            "directory": task["directory"],
            "session_id": task.get("session_id"),
            "summary": task.get("result_text") or "",
            "diff": task.get("diff"),
        }
        return out


@mcp.tool()
def coding_continue_task(task_id: str, prompt: str) -> Dict[str, Any]:
    """
    Send a follow-up prompt on the same OpenCode session as an earlier task.

    Creates a new task_id that reuses the prior session_id.
    """
    if not prompt or not prompt.strip():
        return {"ok": False, "error": "prompt is required"}
    with _tasks_lock:
        parent = _tasks.get(task_id)
        if not parent:
            return {"ok": False, "error": f"unknown task_id: {task_id}"}
        session_id = parent.get("session_id")
        directory = parent["directory"]
        if not session_id:
            return {
                "ok": False,
                "error": "parent task has no session_id yet; wait for it to start",
            }

    new_id = str(uuid.uuid4())
    with _tasks_lock:
        _tasks[new_id] = {
            "status": "pending",
            "directory": directory,
            "prompt": prompt.strip(),
            "session_id": session_id,
            "result_text": None,
            "diff": None,
            "error": None,
            "progress": None,
            "started_at": time.time(),
            "finished_at": None,
            "parent_task_id": task_id,
        }
    _start_worker(new_id)
    return {
        "ok": True,
        "task_id": new_id,
        "parent_task_id": task_id,
        "session_id": session_id,
        "directory": directory,
        "status": "pending",
    }


# Health endpoint for compose healthcheck (FastMCP ASGI mount)
@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "opencode-mcp",
            "opencode_base_url": OPENCODE_BASE_URL,
            "roots": _coding_roots(),
        }
    )


if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))
    transport = os.environ.get("MCP_TRANSPORT", "sse")
    logger.info(
        "Starting opencode-mcp on %s:%s transport=%s roots=%s",
        host,
        port,
        transport,
        _coding_roots(),
    )
    # fastmcp 2.x: run SSE server
    mcp.run(transport=transport, host=host, port=port)
