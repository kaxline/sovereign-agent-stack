#!/usr/bin/env bash
# Temporary LightRAG fixture for eval/kb-query.
#
# Inserts one text document via POST /documents/text (does not scan inputs/),
# then deletes it with DELETE /documents/delete_document. The scripted harness
# wraps kb-query with install/remove; WebUI paste can call these by hand.
#
# Usage:
#   ./scripts/kb-eval-fixture.sh install
#   ./scripts/kb-eval-fixture.sh remove
#   ./scripts/kb-eval-fixture.sh status
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"

SRC="${ROOT}/eval/tool-calling/kb-fixture.md"
FILE_SOURCE="aurora-kb-eval.md"
STATE="${ROOT}/data/hermes/eval/kb-fixture-state.json"
TIMEOUT="${KB_EVAL_FIXTURE_TIMEOUT:-600}"

usage() {
  cat <<'EOF'
Usage: ./scripts/kb-eval-fixture.sh <install|remove|status>

  install   POST /documents/text (aurora-kb-eval), wait until processed
  remove    DELETE that doc by id and drop any leftover inputs/ copy
  status    Print whether the fixture is currently in the hot KB

The scripted harness (make tool-eval-run CASE=kb-query) calls install/remove
around the case. WebUI: install, paste the prompt, then remove.
EOF
}

env_get() {
  local key="$1"
  local default="${2:-}"
  local val=""
  if [[ -f .env ]]; then
    val="$(grep -E "^${key}=" .env 2>/dev/null | head -1 | cut -d= -f2- || true)"
    val="${val%\"}"
    val="${val#\"}"
    val="${val%\'}"
    val="${val#\'}"
  fi
  if [[ -z "$val" ]]; then
    echo "$default"
  else
    echo "$val"
  fi
}

log() { printf '[kb-eval-fixture] %s\n' "$1"; }
die() { printf '[kb-eval-fixture] ERROR: %s\n' "$1" >&2; exit 1; }

require_rag() {
  local profiles
  profiles="$(env_get COMPOSE_PROFILES core)"
  case ",${profiles}," in
    *,rag,*) ;;
    *) die "rag profile is not enabled (COMPOSE_PROFILES=${profiles})" ;;
  esac
  [[ -n "$(env_get WORKSPACE)" ]] || die "WORKSPACE unset in .env"
  [[ -n "$(env_get LIGHTRAG_API_KEY)" ]] || die "LIGHTRAG_API_KEY unset in .env"
}

# Python talks to LightRAG. Subcommands: find, insert, wait, delete, pipeline, write_state.
lr() {
  python3 - "$1" "${2:-}" <<'PY'
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

cmd = sys.argv[1]

src = Path(os.environ["KB_EVAL_SRC"])
file_source = os.environ["KB_EVAL_FILE_SOURCE"]
state_path = Path(os.environ["KB_EVAL_STATE"])
timeout = int(os.environ.get("KB_EVAL_TIMEOUT") or "600")
port = os.environ["KB_EVAL_PORT"]
key = os.environ["KB_EVAL_KEY"]
base = f"http://127.0.0.1:{port}"


def req(method: str, path: str, body=None, timeout_s: int = 30):
    data = None
    headers = {"X-API-Key": key}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw) if raw else {"detail": str(e)}
        except json.JSONDecodeError:
            payload = {"detail": raw.decode("utf-8", "replace")[:400]}
        return e.code, payload


def all_docs():
    status, data = req("GET", "/documents")
    if status != 200:
        raise SystemExit(f"GET /documents failed ({status}): {data}")
    found = []
    statuses = data.get("statuses") or {}
    if isinstance(statuses, dict):
        for group in statuses.values():
            if isinstance(group, list):
                found.extend(x for x in group if isinstance(x, dict))
    return found


def matches(doc: dict) -> bool:
    path = str(doc.get("file_path") or "")
    summary = str(doc.get("content_summary") or "")
    if path.endswith(file_source) or path == file_source:
        return True
    if "aurora-kb-eval" in path.lower() or "aurora-kb-eval" in summary.lower():
        return True
    return False


def fixture_docs():
    return [d for d in all_docs() if matches(d)]


def write_state(extra: dict):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "file_source": file_source,
        "workspace": os.environ.get("KB_EVAL_WORKSPACE") or "",
        **extra,
    }
    state_path.write_text(json.dumps(payload, indent=2) + "\n")


def load_state() -> dict:
    if not state_path.is_file():
        return {}
    try:
        data = json.loads(state_path.read_text())
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def wait_processed(track_id: str | None, doc_id: str | None) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        docs = fixture_docs()
        if track_id:
            docs = [d for d in docs if d.get("track_id") == track_id] or docs
        if doc_id:
            docs = [d for d in docs if d.get("id") == doc_id] or docs
        if docs:
            last = docs[0]
            status = str(last.get("status") or "").lower()
            if status in ("processed", "preprocessed"):
                return last
            if status == "failed":
                raise SystemExit(f"fixture ingest failed: {last.get('error_msg') or last}")
        if track_id:
            st, payload = req("GET", f"/documents/track_status/{track_id}")
            if st == 200:
                text = json.dumps(payload).lower()
                if any(x in text for x in ('"failed"', '"error"', '"cancelled"')):
                    raise SystemExit(f"track {track_id} failed: {payload}")
        time.sleep(2)
    raise SystemExit(f"timed out after {timeout}s waiting for fixture ingest")


def wait_gone(doc_ids: list[str]):
    deadline = time.time() + timeout
    wanted = set(doc_ids)
    while time.time() < deadline:
        remaining = [
            d.get("id")
            for d in fixture_docs()
            if d.get("id") in wanted or matches(d)
        ]
        st, pipe = req("GET", "/documents/pipeline_status")
        busy = bool((pipe or {}).get("busy") or (pipe or {}).get("destructive_busy")) if st == 200 else True
        if not remaining and not busy:
            return
        time.sleep(2)
    raise SystemExit(f"timed out after {timeout}s waiting for fixture delete")


if cmd == "find":
    docs = fixture_docs()
    print(json.dumps(docs))
elif cmd == "status":
    docs = fixture_docs()
    print(json.dumps({"count": len(docs), "docs": docs}, indent=2))
elif cmd == "insert":
    text = src.read_text(encoding="utf-8")
    existing = fixture_docs()
    ready = [d for d in existing if str(d.get("status") or "").lower() in ("processed", "preprocessed")]
    if ready:
        write_state({"doc_id": ready[0].get("id"), "track_id": ready[0].get("track_id")})
        print(json.dumps({"reused": True, "doc_id": ready[0].get("id")}))
        raise SystemExit(0)
    leftover = [d.get("id") for d in existing if d.get("id")]
    if leftover:
        st, payload = req(
            "DELETE",
            "/documents/delete_document",
            {"doc_ids": leftover, "delete_file": True, "delete_llm_cache": True},
        )
        if st == 200 and payload.get("status") in ("deletion_started", "busy"):
            if payload.get("status") == "busy":
                time.sleep(3)
                req(
                    "DELETE",
                    "/documents/delete_document",
                    {"doc_ids": leftover, "delete_file": True, "delete_llm_cache": True},
                )
            wait_gone([x for x in leftover if x])
    st, payload = req(
        "POST",
        "/documents/text",
        {"text": text, "file_source": file_source},
        timeout_s=60,
    )
    if st == 409:
        raise SystemExit(f"insert conflict (409): {payload}")
    if st != 200 or payload.get("status") == "failure":
        raise SystemExit(f"insert failed ({st}): {payload}")
    track_id = payload.get("track_id")
    write_state({"track_id": track_id})
    doc = wait_processed(track_id, None)
    write_state({"doc_id": doc.get("id"), "track_id": track_id or doc.get("track_id")})
    print(json.dumps({"reused": False, "doc_id": doc.get("id"), "track_id": track_id}))
elif cmd == "delete":
    state = load_state()
    ids = [d.get("id") for d in fixture_docs() if d.get("id")]
    if state.get("doc_id") and state["doc_id"] not in ids:
        ids.append(state["doc_id"])
    ids = [i for i in ids if i]
    if not ids:
        if state_path.exists():
            state_path.unlink()
        print(json.dumps({"deleted": [], "already_gone": True}))
        raise SystemExit(0)
    last = {}
    for attempt in range(8):
        st, last = req(
            "DELETE",
            "/documents/delete_document",
            {"doc_ids": ids, "delete_file": True, "delete_llm_cache": True},
        )
        if st == 200 and last.get("status") == "deletion_started":
            break
        if st == 200 and last.get("status") == "busy":
            time.sleep(3)
            continue
        raise SystemExit(f"delete failed ({st}): {last}")
    else:
        raise SystemExit(f"delete stayed busy: {last}")
    wait_gone(ids)
    if state_path.exists():
        state_path.unlink()
    print(json.dumps({"deleted": ids}))
else:
    raise SystemExit(f"unknown lr cmd: {cmd}")
PY
}

export_lr_env() {
  export KB_EVAL_SRC="$SRC"
  export KB_EVAL_FILE_SOURCE="$FILE_SOURCE"
  export KB_EVAL_STATE="$STATE"
  export KB_EVAL_TIMEOUT="$TIMEOUT"
  export KB_EVAL_PORT
  export KB_EVAL_KEY
  export KB_EVAL_WORKSPACE
  KB_EVAL_PORT="$(env_get PORT 9621)"
  KB_EVAL_KEY="$(env_get LIGHTRAG_API_KEY)"
  KB_EVAL_WORKSPACE="$(env_get WORKSPACE)"
}

remove_inputs_copy() {
  local dest
  dest="$(resolve_data_root_path "")/inputs/$(env_get WORKSPACE)/${FILE_SOURCE}"
  if [[ -f "$dest" ]]; then
    rm -f "$dest"
    log "removed leftover inputs copy ${dest}"
  fi
}

cmd_install() {
  require_rag
  [[ -f "$SRC" ]] || die "missing ${SRC}"
  export_lr_env
  remove_inputs_copy
  log "inserting ${FILE_SOURCE} via POST /documents/text (timeout ${TIMEOUT}s)"
  local out
  out="$(lr insert)" || die "install failed"
  log "ready ${out}"
}

cmd_remove() {
  require_rag
  export_lr_env
  remove_inputs_copy
  local out
  out="$(lr delete)" || die "remove failed"
  log "cleaned ${out}"
}

cmd_status() {
  require_rag
  export_lr_env
  lr status
}

main() {
  local cmd="${1:-install}"
  case "$cmd" in
    install) cmd_install ;;
    remove|uninstall|clean) cmd_remove ;;
    status) cmd_status ;;
    -h|--help|help) usage ;;
    *) usage >&2; die "unknown command: ${cmd}" ;;
  esac
}

main "$@"
