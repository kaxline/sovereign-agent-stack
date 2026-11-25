#!/usr/bin/env bash
# Corpus lifecycle: create / use / list / ingest / destroy for LightRAG workspaces.
# One hot corpus at a time (WORKSPACE in .env). Cold corpora live on disk.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"

DATA_ROOT="$(resolve_data_root_path)"
REGISTRY="${DATA_ROOT}/corpora/registry.json"

usage() {
  cat <<'EOF'
Usage: ./scripts/corpus.sh <command> [slug]

Commands:
  create <slug>     Create input + storage dirs and a registry row (does not switch)
  use <slug>        Make slug the hot WORKSPACE; recreate LightRAG (+ Hermes if changed)
  list              List corpora; mark the active WORKSPACE
  ingest [slug]     Scan ASSISTANT_DATA_ROOT/inputs/<slug> into the hot LightRAG (slug must be active)
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

validate_slug() {
  local slug="$1"
  [[ "$slug" =~ ^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$ ]] \
    || die "Invalid corpus slug: $slug (use letters, numbers, hyphens)"
}

require_rag_profile() {
  has_compose_profile rag \
    || die "rag profile is not enabled (COMPOSE_PROFILES=$(env_get COMPOSE_PROFILES core)). Enable it: ./scripts/addon.sh enable lightrag"
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

# registry CMD ARGS: scripts/lib/corpus_registry.py on REGISTRY.
registry() { python3 "${ROOT}/scripts/lib/corpus_registry.py" "$REGISTRY" "$@"; }

# Ensure registry exists; backfill current WORKSPACE if dirs exist but registry does not.
ensure_registry() {
  mkdir -p "${DATA_ROOT}/corpora"
  local out
  out="$(registry ensure "$DATA_ROOT" "$(env_get WORKSPACE)" "$(env_get EMBEDDING_MODEL)" "$(env_get EMBEDDING_DIM)")"
  if [[ "$out" == backfilled:* ]]; then
    log "Backfilled registry with existing WORKSPACE '${out#backfilled:}'"
  fi
}

registry_has() { registry has "$1"; }
registry_add() { registry add "$1" "$(env_get EMBEDDING_MODEL)" "$(env_get EMBEDDING_DIM)"; }
registry_remove() { registry remove "$1"; }
registry_dim_for() { registry dim "$1"; }

corpus_dirs_exist() {
  local slug="$1"
  [[ -d "${DATA_ROOT}/inputs/${slug}" ]]
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
  mkdir -p "${DATA_ROOT}/inputs/${slug}" "${DATA_ROOT}/rag_storage/${slug}"
  seed_file "${SEED_DIR}/getting-started.md" "${DATA_ROOT}/inputs/${slug}/getting-started.md" "WORKSPACE=${slug}"
  registry_add "$slug"
  log "Created corpus '${slug}'"
  log "  inputs:  ${DATA_ROOT}/inputs/${slug}/"
  log "  storage: ${DATA_ROOT}/rag_storage/${slug}/"
  log "Not switched. Run: ./scripts/corpus.sh use ${slug}"
}

cmd_list() {
  ensure_registry
  local active
  active="$(env_get WORKSPACE)"
  registry list "$DATA_ROOT" "$active"
}

wait_lightrag_healthy() {
  local port key
  port="$(env_get PORT 9621)"
  key="$(env_get LIGHTRAG_API_KEY)"
  for _ in $(seq 1 60); do
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
    die "Corpus '${slug}' not found (missing ${DATA_ROOT}/inputs/${slug}/). Create it first."
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
  local active port key resp track_id status
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
  corpus_dirs_exist "$slug" || die "Missing ${DATA_ROOT}/inputs/${slug}/"

  port="$(env_get PORT 9621)"
  key="$(env_get LIGHTRAG_API_KEY)"
  [[ -n "$key" ]] || die "LIGHTRAG_API_KEY is unset"

  log "Scanning ${DATA_ROOT}/inputs/${slug}/ via LightRAG"
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
  for _ in $(seq 1 180); do
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
  local active confirm neo_user neo_pass
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
  echo "  ${DATA_ROOT}/inputs/${slug}/"
  echo "  ${DATA_ROOT}/rag_storage/${slug}/"
  echo "  Neo4j nodes labeled :\`${slug}\`"
  echo "  registry entry for ${slug}"
  read -r -p "Type the slug '${slug}' to confirm: " confirm
  if [[ "$confirm" != "$slug" ]]; then
    die "Aborted (confirmation did not match)"
  fi

  rm -rf "${DATA_ROOT}/inputs/${slug}" "${DATA_ROOT}/rag_storage/${slug}"
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
