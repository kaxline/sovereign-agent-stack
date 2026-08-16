#!/usr/bin/env bash
# Corpus lifecycle: create / use / list / ingest / destroy for LightRAG workspaces.
# One hot corpus at a time (WORKSPACE in .env). Cold corpora live on disk.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

REGISTRY="$ROOT/data/corpora/registry.json"

usage() {
  cat <<'EOF'
Usage: ./scripts/corpus.sh <command> [slug]

Commands:
  create <slug>     Create input + storage dirs and a registry row (does not switch)
  use <slug>        Make slug the hot WORKSPACE; recreate LightRAG (+ Hermes if changed)
  list              List corpora; mark the active WORKSPACE
  ingest [slug]     Scan data/inputs/<slug> into the hot LightRAG (slug must be active)
  destroy <slug>    Delete dirs, Neo4j label data, and registry row (not while active)

Examples:
  ./scripts/corpus.sh create demo-a
  ./scripts/corpus.sh use demo-a
  ./scripts/corpus.sh ingest
  make corpus-use SLUG=demo-b
EOF
}

log() { printf '[corpus] %s\n' "$1"; }
die() { printf '[corpus] ERROR: %s\n' "$1" >&2; exit 1; }

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

upsert_env() {
  local file="$1"
  local key="$2"
  local value="$3"
  touch "$file"
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    local tmp
    tmp="$(mktemp)"
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" == "${key}="* ]]; then
        printf '%s=%s\n' "$key" "$value"
      else
        printf '%s\n' "$line"
      fi
    done < "$file" > "$tmp"
    mv "$tmp" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

validate_slug() {
  local slug="$1"
  [[ "$slug" =~ ^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$ ]] \
    || die "Invalid corpus slug: $slug (use letters, numbers, hyphens)"
}

require_rag_profile() {
  local profiles
  profiles="$(env_get COMPOSE_PROFILES core)"
  case ",${profiles}," in
    *,rag,*) ;;
    *) die "rag profile is not enabled (COMPOSE_PROFILES=${profiles}). Re-run: ./scripts/setup.sh --rag" ;;
  esac
}

refuse_backend_workspace_overrides() {
  local neo pg
  neo="$(env_get NEO4J_WORKSPACE)"
  pg="$(env_get POSTGRES_WORKSPACE)"
  if [[ -n "$neo" ]]; then
    die "NEO4J_WORKSPACE is set in .env (${neo}). Unset it — it collapses every corpus into one Neo4j label."
  fi
  if [[ -n "$pg" ]]; then
    die "POSTGRES_WORKSPACE is set in .env (${pg}). Unset it — it collapses workspace isolation."
  fi
}

create_getting_started() {
  local workspace="$1"
  local dest="$ROOT/data/inputs/${workspace}/getting-started.md"
  [[ -f "$dest" ]] && return 0
  cat > "$dest" <<EOF
# Getting started

Welcome to your local knowledge base (**${workspace}**).

Drop documents into this folder — PDF, DOCX, MD, TXT, and more — then either:

1. Upload via the LightRAG web UI at http://localhost:9621/webui/, or
2. Run: make corpus-ingest SLUG=${workspace}
   (after \`make corpus-use SLUG=${workspace}\`)

After ingestion, explore the knowledge graph in the LightRAG UI or Neo4j Browser.
EOF
  log "Wrote $dest"
}

# Ensure registry exists; backfill current WORKSPACE if dirs exist but registry does not.
ensure_registry() {
  mkdir -p "$ROOT/data/corpora"
  local out
  out="$(python3 - "$REGISTRY" "$ROOT" <<'PY'
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

registry_path = Path(sys.argv[1])
root = Path(sys.argv[2])

def env_get(key, default=""):
    env_file = root / ".env"
    if not env_file.is_file():
        return default
    for line in env_file.read_text().splitlines():
        if line.startswith(key + "="):
            val = line.split("=", 1)[1].strip().strip("'\"")
            return val or default
    return default

def load():
    if registry_path.is_file():
        try:
            data = json.loads(registry_path.read_text())
            if isinstance(data, dict) and isinstance(data.get("corpora"), list):
                return data
        except json.JSONDecodeError:
            pass
    return {"corpora": []}

data = load()
slugs = {c.get("slug") for c in data["corpora"] if isinstance(c, dict)}
workspace = env_get("WORKSPACE")
inputs = root / "data" / "inputs"
if workspace and workspace not in slugs and (inputs / workspace).is_dir():
    data["corpora"].append({
        "slug": workspace,
        "embedding_model": env_get("EMBEDDING_MODEL"),
        "embedding_dim": env_get("EMBEDDING_DIM"),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"backfilled:{workspace}")
elif not registry_path.is_file():
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(data, indent=2) + "\n")
PY
)"
  if [[ "$out" == backfilled:* ]]; then
    log "Backfilled registry with existing WORKSPACE '${out#backfilled:}'"
  fi
}

registry_has() {
  local slug="$1"
  python3 - "$REGISTRY" "$slug" <<'PY'
import json, sys
from pathlib import Path
path, slug = Path(sys.argv[1]), sys.argv[2]
if not path.is_file():
    raise SystemExit(1)
data = json.loads(path.read_text())
raise SystemExit(0 if any(c.get("slug") == slug for c in data.get("corpora", [])) else 1)
PY
}

registry_add() {
  local slug="$1"
  local model dim
  model="$(env_get EMBEDDING_MODEL)"
  dim="$(env_get EMBEDDING_DIM)"
  python3 - "$REGISTRY" "$slug" "$model" "$dim" <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
slug, model, dim = sys.argv[2], sys.argv[3], sys.argv[4]
data = {"corpora": []}
if path.is_file():
    try:
        data = json.loads(path.read_text())
        if not isinstance(data.get("corpora"), list):
            data = {"corpora": []}
    except json.JSONDecodeError:
        data = {"corpora": []}
if any(c.get("slug") == slug for c in data["corpora"]):
    raise SystemExit(0)
data["corpora"].append({
    "slug": slug,
    "embedding_model": model,
    "embedding_dim": dim,
    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
})
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(data, indent=2) + "\n")
PY
}

registry_remove() {
  local slug="$1"
  python3 - "$REGISTRY" "$slug" <<'PY'
import json, sys
from pathlib import Path
path, slug = Path(sys.argv[1]), sys.argv[2]
if not path.is_file():
    raise SystemExit(0)
data = json.loads(path.read_text())
data["corpora"] = [c for c in data.get("corpora", []) if c.get("slug") != slug]
path.write_text(json.dumps(data, indent=2) + "\n")
PY
}

registry_dim_for() {
  local slug="$1"
  python3 - "$REGISTRY" "$slug" <<'PY'
import json, sys
from pathlib import Path
path, slug = Path(sys.argv[1]), sys.argv[2]
if not path.is_file():
    raise SystemExit(0)
data = json.loads(path.read_text())
for c in data.get("corpora", []):
    if c.get("slug") == slug:
        print(c.get("embedding_dim") or "")
        raise SystemExit(0)
raise SystemExit(0)
PY
}

corpus_dirs_exist() {
  local slug="$1"
  [[ -d "$ROOT/data/inputs/${slug}" ]]
}

cmd_create() {
  local slug="${1:-}"
  [[ -n "$slug" ]] || die "Usage: ./scripts/corpus.sh create <slug>"
  validate_slug "$slug"
  ensure_registry
  if corpus_dirs_exist "$slug" && registry_has "$slug"; then
    log "Corpus '${slug}' already exists"
    return 0
  fi
  mkdir -p "$ROOT/data/inputs/${slug}" "$ROOT/data/rag_storage/${slug}"
  create_getting_started "$slug"
  registry_add "$slug"
  log "Created corpus '${slug}'"
  log "  inputs:  data/inputs/${slug}/"
  log "  storage: data/rag_storage/${slug}/"
  log "Not switched. Run: ./scripts/corpus.sh use ${slug}"
}

cmd_list() {
  ensure_registry
  local active
  active="$(env_get WORKSPACE)"
  python3 - "$REGISTRY" "$ROOT" "$active" <<'PY'
import json, sys
from pathlib import Path

registry_path, root, active = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
inputs = root / "data" / "inputs"
slugs = set()
rows = {}
if registry_path.is_file():
    data = json.loads(registry_path.read_text())
    for c in data.get("corpora", []):
        slug = c.get("slug")
        if slug:
            slugs.add(slug)
            rows[slug] = c
if inputs.is_dir():
    try:
        for p in inputs.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                slugs.add(p.name)
    except PermissionError:
        pass

if not slugs:
    print("(no corpora)")
    raise SystemExit(0)

for slug in sorted(slugs):
    mark = "*" if slug == active else " "
    row = rows.get(slug, {})
    dim = row.get("embedding_dim") or "?"
    model = row.get("embedding_model") or "?"
    try:
        on_disk = "yes" if (inputs / slug).is_dir() else "missing"
    except PermissionError:
        on_disk = "?"
    print(f"{mark} {slug}  dim={dim}  model={model}  inputs={on_disk}")
if active:
    print(f"\nactive WORKSPACE={active}")
PY
}

wait_lightrag_healthy() {
  local port key i
  port="$(env_get PORT 9621)"
  key="$(env_get LIGHTRAG_API_KEY)"
  for i in $(seq 1 60); do
    if curl -sf -H "X-API-Key: ${key}" "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      log "LightRAG healthy on :${port}"
      return 0
    fi
    sleep 2
  done
  die "LightRAG did not become healthy on :${port} within 120s"
}

cmd_use() {
  local slug="${1:-}"
  local prev need_hermes
  [[ -n "$slug" ]] || die "Usage: ./scripts/corpus.sh use <slug>"
  validate_slug "$slug"
  require_rag_profile
  refuse_backend_workspace_overrides
  ensure_registry

  if ! corpus_dirs_exist "$slug"; then
    die "Corpus '${slug}' not found (missing data/inputs/${slug}/). Create it first."
  fi
  if ! registry_has "$slug"; then
    log "Registry missing '${slug}' — adding from current embedding settings"
    registry_add "$slug"
  fi

  local reg_dim cur_dim
  reg_dim="$(registry_dim_for "$slug")"
  cur_dim="$(env_get EMBEDDING_DIM)"
  if [[ -n "$reg_dim" && -n "$cur_dim" && "$reg_dim" != "$cur_dim" ]]; then
    die "Corpus '${slug}' was created with EMBEDDING_DIM=${reg_dim}, but .env has EMBEDDING_DIM=${cur_dim}. Fix the mismatch before switching (changing dim corrupts the vector store)."
  fi

  prev="$(env_get WORKSPACE)"
  need_hermes=0
  if [[ "$prev" != "$slug" ]]; then
    need_hermes=1
  fi

  [[ -f .env ]] || die ".env missing — run ./scripts/setup.sh"
  upsert_env .env WORKSPACE "$slug"
  upsert_env .env WEBUI_TITLE "'${slug}'"

  if [[ "$need_hermes" -eq 0 ]]; then
    log "Already active WORKSPACE=${slug}; recreating LightRAG only"
    docker compose up -d --force-recreate lightrag lightrag-mcp
  else
    log "Switching WORKSPACE ${prev:-'(unset)'} -> ${slug}"
    docker compose up -d --force-recreate lightrag lightrag-mcp hermes
  fi
  wait_lightrag_healthy
  log "active = ${slug}"
}

cmd_ingest() {
  local slug="${1:-}"
  local active port key resp track_id status i
  require_rag_profile
  refuse_backend_workspace_overrides
  active="$(env_get WORKSPACE)"
  [[ -n "$active" ]] || die "WORKSPACE is unset in .env"
  if [[ -z "$slug" ]]; then
    slug="$active"
  fi
  validate_slug "$slug"
  if [[ "$slug" != "$active" ]]; then
    die "Corpus '${slug}' is not hot (active WORKSPACE=${active}). Run: ./scripts/corpus.sh use ${slug}"
  fi
  corpus_dirs_exist "$slug" || die "Missing data/inputs/${slug}/"

  port="$(env_get PORT 9621)"
  key="$(env_get LIGHTRAG_API_KEY)"
  [[ -n "$key" ]] || die "LIGHTRAG_API_KEY is unset"

  log "Scanning data/inputs/${slug}/ via LightRAG"
  resp="$(curl -sf -X POST -H "X-API-Key: ${key}" "http://127.0.0.1:${port}/documents/scan" || true)"
  if [[ -z "$resp" ]]; then
    die "Scan request failed (is LightRAG up? try: make corpus-use SLUG=${slug})"
  fi
  log "Scan response: ${resp}"

  track_id="$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(d.get("track_id") or d.get("tracking_id") or "")' "$resp" 2>/dev/null || true)"
  if [[ -z "$track_id" ]]; then
    log "No track_id in response — assuming scan accepted; check LightRAG UI / pipeline status"
    return 0
  fi

  log "Polling track_status/${track_id}"
  for i in $(seq 1 180); do
    status="$(curl -sf -H "X-API-Key: ${key}" "http://127.0.0.1:${port}/documents/track_status/${track_id}" 2>/dev/null || true)"
    if [[ -z "$status" ]]; then
      sleep 2
      continue
    fi
    local rc=0
    set +e
    python3 - "$status" <<'PY'
import json, sys
raw = sys.argv[1]
try:
    d = json.loads(raw)
except json.JSONDecodeError:
    print(raw[:200])
    raise SystemExit(1)
# LightRAG shapes vary; accept common terminal markers.
text = json.dumps(d).lower()
status = str(d.get("status") or d.get("pipeline_status") or d.get("track_status") or "")
print(f"  status={status or 'unknown'}")
if any(x in text for x in ('"failed"', '"error"', '"cancelled"')):
    print(raw[:500], file=sys.stderr)
    raise SystemExit(2)
done_markers = ("completed", "complete", "success", "finished", "done", "idle")
if status.lower() in done_markers:
    raise SystemExit(0)
# Some APIs nest documents with per-doc status and omit top-level status when idle.
docs = d.get("documents") or d.get("docs") or []
if isinstance(docs, list) and docs:
    states = [str(x.get("status", "")).lower() for x in docs if isinstance(x, dict)]
    if states and all(s in done_markers or s in ("processed", "indexed") for s in states):
        raise SystemExit(0)
raise SystemExit(1)
PY
    rc=$?
    set -e
    if [[ $rc -eq 0 ]]; then
      log "Ingest finished for track ${track_id}"
      return 0
    fi
    if [[ $rc -eq 2 ]]; then
      die "Ingest failed for track ${track_id}"
    fi
    sleep 2
  done
  die "Timed out waiting for ingest track ${track_id}"
}

cmd_destroy() {
  local slug="${1:-}"
  local active confirm user neo_user neo_pass
  [[ -n "$slug" ]] || die "Usage: ./scripts/corpus.sh destroy <slug>"
  validate_slug "$slug"
  ensure_registry
  active="$(env_get WORKSPACE)"
  if [[ "$slug" == "$active" ]]; then
    die "Refusing to destroy active corpus '${slug}'. Switch away first: ./scripts/corpus.sh use <other>"
  fi
  if ! corpus_dirs_exist "$slug" && ! registry_has "$slug"; then
    die "Corpus '${slug}' not found"
  fi

  echo "This will permanently delete:"
  echo "  data/inputs/${slug}/"
  echo "  data/rag_storage/${slug}/"
  echo "  Neo4j nodes labeled :\`${slug}\`"
  echo "  registry entry for ${slug}"
  read -r -p "Type the slug '${slug}' to confirm: " confirm
  if [[ "$confirm" != "$slug" ]]; then
    die "Aborted (confirmation did not match)"
  fi

  rm -rf "$ROOT/data/inputs/${slug}" "$ROOT/data/rag_storage/${slug}"
  log "Removed input and storage directories"

  if docker compose ps --status running neo4j 2>/dev/null | grep -q neo4j; then
    neo_user="$(env_get NEO4J_USERNAME neo4j)"
    neo_pass="$(env_get NEO4J_PASSWORD)"
    if [[ -n "$neo_pass" ]]; then
      # Backtick-escape the label the same way LightRAG does (double backticks).
      local label
      label="${slug//\`/\`\`}"
      if docker compose exec -T neo4j \
        cypher-shell -u "$neo_user" -p "$neo_pass" \
        "MATCH (n:\`${label}\`) DETACH DELETE n" >/dev/null 2>&1; then
        log "Deleted Neo4j nodes with label :${slug}"
      else
        log "WARN: Neo4j cleanup failed or no nodes matched; dirs are already gone"
      fi
    else
      log "WARN: NEO4J_PASSWORD unset — skipped graph cleanup"
    fi
  else
    log "WARN: neo4j not running — skipped graph cleanup (start rag profile and re-run Cypher if needed)"
  fi

  registry_remove "$slug"
  log "Destroyed corpus '${slug}'"
}

main() {
  local cmd="${1:-}"
  shift || true
  case "$cmd" in
    create) cmd_create "$@" ;;
    use) cmd_use "$@" ;;
    list|ls) cmd_list "$@" ;;
    ingest) cmd_ingest "$@" ;;
    destroy|rm) cmd_destroy "$@" ;;
    -h|--help|help|"") usage; [[ -n "$cmd" ]] || exit 1; exit 0 ;;
    *) die "Unknown command: $cmd (try --help)" ;;
  esac
}

main "$@"
