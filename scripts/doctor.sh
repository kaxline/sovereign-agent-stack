#!/usr/bin/env bash
# Preflight checks for the local agent stack. Same logic a desktop launcher will need.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PASS=0
FAIL=0
WARN=0

ok() { printf '  OK  %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf ' FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }
warn() { printf ' WARN %s\n' "$1"; WARN=$((WARN + 1)); }

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

profiles_csv="$(env_get COMPOSE_PROFILES core)"
IFS=',' read -r -a PROFILES <<< "$profiles_csv"
has_profile() {
  local want="$1"
  local p
  for p in "${PROFILES[@]}"; do
    p="$(echo "$p" | tr -d '[:space:]')"
    [[ "$p" == "$want" ]] && return 0
  done
  return 1
}

echo "=== Local agent stack doctor ==="
echo "Profiles: ${profiles_csv}"
echo

# --- Docker ---
if command -v docker >/dev/null 2>&1; then
  ok "docker is installed"
else
  bad "docker is not installed"
fi

if docker compose version >/dev/null 2>&1; then
  ok "docker compose v2 is available"
else
  bad "docker compose v2 is not available"
fi

if docker info >/dev/null 2>&1; then
  ok "Docker daemon is running"
else
  bad "Docker daemon is not running"
fi

# --- Required files ---
if [[ -f .env ]]; then
  ok ".env exists"
else
  bad ".env missing — run ./scripts/setup.sh"
fi

if [[ -f searxng/settings.local.yml ]]; then
  ok "searxng/settings.local.yml exists"
else
  bad "searxng/settings.local.yml missing — run ./scripts/setup.sh or make ensure-local"
fi

if [[ -f opencode/opencode.local.json ]]; then
  ok "opencode/opencode.local.json exists"
else
  warn "opencode/opencode.local.json missing (needed for coding profile)"
fi

if has_profile core; then
  if [[ -f compose/hermes/api-server.env ]]; then
    ok "compose/hermes/api-server.env exists"
  else
    bad "compose/hermes/api-server.env missing — run ./scripts/setup.sh"
  fi
  if [[ -f compose/hermes/browser.env ]]; then
    ok "compose/hermes/browser.env exists"
  else
    bad "compose/hermes/browser.env missing — run ./scripts/setup.sh"
  fi
  dash_pw="$(env_get HERMES_DASHBOARD_PASSWORD)"
  if [[ -z "$dash_pw" || "$dash_pw" == change-me* ]]; then
    bad "HERMES_DASHBOARD_PASSWORD is unset or still a placeholder"
  else
    ok "HERMES_DASHBOARD_PASSWORD is set"
  fi
fi

# --- Ports (best-effort; skip if lsof unavailable) ---
check_port() {
  local port="$1"
  local label="$2"
  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    # Listening is fine if it is our own stack; warn either way so the user notices.
    warn "port ${port} (${label}) is already in use"
  else
    ok "port ${port} (${label}) is free"
  fi
}

if has_profile core; then
  check_port "$(env_get HERMES_WEBUI_PORT 8787)" "Hermes WebUI"
  check_port "$(env_get HERMES_DASHBOARD_PORT 9119)" "Hermes dashboard"
  check_port "$(env_get SEARXNG_PORT 8080)" "SearXNG"
fi
if has_profile rag; then
  check_port "$(env_get PORT 9621)" "LightRAG"
  check_port 7474 "Neo4j browser"
fi
if has_profile automation; then
  check_port 5678 "n8n"
  check_port "$(env_get GPTR_PORT 8000)" "GPT Researcher"
fi
if has_profile coding; then
  check_port "$(env_get OPENCODE_PORT 4096)" "OpenCode"
fi
if has_profile ollama; then
  check_port "$(env_get OLLAMA_HOST_PORT 11434)" "Ollama"
fi

# --- Compose config ---
if [[ -f .env ]] && docker compose version >/dev/null 2>&1; then
  if docker compose config >/dev/null 2>&1; then
    ok "docker compose config validates"
  else
    bad "docker compose config failed — check .env and local overlay files"
  fi
fi

# --- Model server reachability from inside a container ---
LLM_HOST="$(env_get LLM_BINDING_HOST http://host.docker.internal:1234/v1)"
LLM_MODEL="$(env_get LLM_MODEL)"
EMBED_HOST="$(env_get EMBEDDING_BINDING_HOST "$LLM_HOST")"
EMBED_MODEL="$(env_get EMBEDDING_MODEL)"
EMBED_DIM="$(env_get EMBEDDING_DIM)"
LLM_BINDING="$(env_get LLM_BINDING openai)"

probe_url() {
  local url="$1"
  docker run --rm --add-host=host.docker.internal:host-gateway curlimages/curl:8.5.0 \
    -sf --max-time 8 "$url" >/dev/null 2>&1
}

echo
echo "--- Model server (probed from a container) ---"
echo "LLM_BINDING_HOST=${LLM_HOST}"

if ! docker info >/dev/null 2>&1; then
  warn "Skipping in-container probes (Docker not running)"
else
  # Normalize probe endpoints
  if [[ "$LLM_BINDING" == "ollama" ]]; then
    base="${LLM_HOST%/}"
    tags_url="${base}/api/tags"
    if probe_url "$tags_url"; then
      ok "Ollama reachable at ${base} (from container)"
      if [[ -n "$LLM_MODEL" ]]; then
        if docker run --rm --add-host=host.docker.internal:host-gateway curlimages/curl:8.5.0 \
          -sf --max-time 8 "$tags_url" 2>/dev/null | grep -Fq "$LLM_MODEL"; then
          ok "chat model '${LLM_MODEL}' present in Ollama tags"
        else
          # Ollama tags use name without always matching full id; soft-warn
          warn "chat model '${LLM_MODEL}' not found in /api/tags — pull it or fix LLM_MODEL"
        fi
      fi
    else
      bad "Ollama not reachable at ${base} from a container"
      echo "       Fix: start Ollama, or enable --ollama, and ensure the URL is correct"
    fi
  else
    # OpenAI-compatible
    models_url="${LLM_HOST%/}/models"
    # Some servers want /v1/models already in host
    if [[ "$LLM_HOST" == */v1 ]]; then
      models_url="${LLM_HOST}/models"
    elif [[ "$LLM_HOST" == */v1/ ]]; then
      models_url="${LLM_HOST}models"
    fi
    if probe_url "$models_url"; then
      ok "OpenAI-compatible server reachable (${models_url})"
      if [[ -n "$LLM_MODEL" ]]; then
        if docker run --rm --add-host=host.docker.internal:host-gateway curlimages/curl:8.5.0 \
          -sf --max-time 8 "$models_url" 2>/dev/null | grep -Fq "$LLM_MODEL"; then
          ok "chat model '${LLM_MODEL}' listed by /models"
        else
          warn "chat model '${LLM_MODEL}' not found in /models — load it on your model server / fix LLM_MODEL"
        fi
      fi
      if [[ -n "$EMBED_MODEL" ]]; then
        emb_models="$models_url"
        if [[ "$EMBED_HOST" != "$LLM_HOST" ]]; then
          if [[ "$EMBED_HOST" == */v1 ]]; then
            emb_models="${EMBED_HOST}/models"
          elif [[ "$(env_get EMBEDDING_BINDING openai)" == "ollama" ]]; then
            emb_models="${EMBED_HOST%/}/api/tags"
          else
            emb_models="${EMBED_HOST%/}/models"
          fi
        fi
        if docker run --rm --add-host=host.docker.internal:host-gateway curlimages/curl:8.5.0 \
          -sf --max-time 8 "$emb_models" 2>/dev/null | grep -Fq "$EMBED_MODEL"; then
          ok "embedding model '${EMBED_MODEL}' listed"
        else
          warn "embedding model '${EMBED_MODEL}' not found — load it or fix EMBEDDING_MODEL"
        fi
      fi
    else
      bad "Model server not reachable at ${models_url} from a container"
      echo "       #1 gotcha: host server must listen beyond localhost (LM Studio: Serve on Local Network; Ollama: OLLAMA_HOST=0.0.0.0)"
      echo "       Host localhost is NOT the container's localhost — use host.docker.internal"
    fi
  fi

  if [[ -z "$EMBED_DIM" ]]; then
    warn "EMBEDDING_DIM is unset — a mismatch silently corrupts the vector store"
  else
    ok "EMBEDDING_DIM=${EMBED_DIM} (must match the embedding model exactly)"
  fi
fi

echo
echo "=== Summary: ${PASS} ok, ${WARN} warnings, ${FAIL} failures ==="
if [[ "$FAIL" -gt 0 ]]; then
  echo "Fix the failures above, then re-run: make doctor"
  exit 1
fi
echo "Ready for: make up"
exit 0
