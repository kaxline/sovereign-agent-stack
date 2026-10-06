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

# --- User data root ---
_DR="$(env_get ASSISTANT_DATA_ROOT ./data)"
if [[ "$_DR" != /* ]]; then
  _DR_ABS="${ROOT}/${_DR#./}"
else
  _DR_ABS="$_DR"
fi
if [[ -d "$_DR_ABS" ]]; then
  ok "ASSISTANT_DATA_ROOT=${_DR} (${_DR_ABS})"
else
  warn "ASSISTANT_DATA_ROOT=${_DR} missing — run: make data-dir-set DIR=${_DR}"
fi
if [[ -d "${ROOT}/data/hermes" ]]; then
  ok "Hermes state at ./data/hermes"
else
  warn "./data/hermes missing — run ./scripts/setup.sh"
fi

# --- Project file context ---
echo
echo "--- Project file context ---"
_PROJ="${_DR_ABS}/projects"
_CODING_PRIMARY="$(env_get OPENCODE_WORKSPACE_HOST)"
_CODING_EXTRA="$(env_get CODING_EXTRA_ROOTS)"
_CONTEXT_EXTRA="$(env_get CONTEXT_EXTRA_ROOTS)"
if [[ ! -d "$_PROJ" ]]; then
  warn "projects dir missing (${_PROJ})"
else
  ok "projects dir ${_PROJ}"
  _any_project=0
  shopt -s nullglob
  for _child in "$_PROJ"/*; do
    [[ -d "$_child" ]] || continue
    _any_project=1
    _slug="$(basename "$_child")"
    if [[ -f "${_child}/INDEX.md" || -f "${_child}/sources.yaml" ]]; then
      if python3 "${ROOT}/scripts/project-index.py" --root "$_child" --check >/dev/null 2>&1; then
        ok "INDEX fresh: ${_slug}"
      else
        _idx_err="$(python3 "${ROOT}/scripts/project-index.py" --root "$_child" --check 2>&1 | tr '\n' ' ' || true)"
        warn "INDEX stale or invalid (${_slug}) — run: make project-index PROJECT=${_slug}"
        [[ -n "$_idx_err" ]] && warn "  ${_idx_err}"
      fi
    fi
  done
  shopt -u nullglob
  if [[ "$_any_project" -eq 0 ]]; then
    ok "no library projects yet"
  fi
  while IFS=$'\t' read -r _lvl _msg || [[ -n "${_lvl:-}" ]]; do
    [[ -n "$_lvl" ]] || continue
    if [[ "$_lvl" == "OK" ]]; then
      ok "$_msg"
    else
      warn "$_msg"
    fi
  done < <(
    python3 "${ROOT}/scripts/lib/project_sources.py" doctor-visibility \
      "$_PROJ" "${_CODING_PRIMARY}" "${_CODING_EXTRA}" "${_CONTEXT_EXTRA}" \
      || true
  )
fi
unset _DR _DR_ABS _PROJ _CODING_PRIMARY _CODING_EXTRA _CONTEXT_EXTRA \
  _any_project _child _slug _idx_err _lvl _msg

# --- Tool-search pins (browser / default) ---
# always_include is written by bootstrap; a stack overlay makes it live for
# MCP pins. tools.tool_search.defer is the Hermes v2026.9+ knob for core
# tools (session_search must not be on it).
# Parse YAML lists without PyYAML so a missing host package cannot false-OK.
if has_profile core; then
  echo
  echo "--- Tool-search pins ---"
  yaml_list_has() {
    python3 - "$1" "$2" "$3" <<'PY'
import sys
from pathlib import Path

cfg, dotted, want = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = cfg.read_text()
parts = dotted.split(".")
lines = text.splitlines()
idx = 0
min_indent = 0
for part in parts:
    found = False
    while idx < len(lines):
        line = lines[idx]
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if stripped.startswith("#") or not stripped:
            idx += 1
            continue
        if indent < min_indent:
            break
        if indent == min_indent and stripped.startswith(part + ":"):
            found = True
            idx += 1
            min_indent = indent + 2
            break
        idx += 1
    if not found:
        raise SystemExit(1)
while idx < len(lines):
    line = lines[idx]
    stripped = line.lstrip(" ")
    indent = len(line) - len(stripped)
    if stripped.startswith("#") or not stripped:
        idx += 1
        continue
    if indent < min_indent:
        break
    if stripped.startswith("- "):
        item = stripped[2:].strip().strip("'\"")
        if item == want:
            raise SystemExit(0)
    idx += 1
raise SystemExit(1)
PY
  }
  check_always_include() {
    local cfg="$1"
    local label="$2"
    shift 2
    if [[ ! -f "$cfg" ]]; then
      warn "${label} config missing (${cfg}) — run hermes-browser-bootstrap"
      return 0
    fi
    local missing=()
    local want
    for want in "$@"; do
      if ! yaml_list_has "$cfg" "tools.tool_search.always_include" "$want"; then
        missing+=("$want")
      fi
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
      ok "${label} tools.tool_search.always_include has SearXNG + session_search"
    else
      warn "${label} always_include missing: ${missing[*]} — re-run hermes-api-bootstrap / hermes-browser-bootstrap"
    fi
  }
  check_always_include "data/hermes/profiles/browser/config.yaml" "browser" \
    session_search searxng_web_search mcp__searxng__searxng_web_search \
    web_url_read mcp__searxng__web_url_read
  check_always_include "data/hermes/config.yaml" "default" \
    session_search searxng_web_search mcp__searxng__searxng_web_search
  check_session_search_undeferred() {
    local cfg="$1"
    local label="$2"
    if [[ ! -f "$cfg" ]]; then
      return 0
    fi
    if yaml_list_has "$cfg" "tools.tool_search.defer" "session_search"; then
      warn "${label} tools.tool_search.defer still lists session_search — re-run hermes-*-bootstrap"
      return 0
    fi
    if python3 - "$cfg" <<'PY'
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
in_tool_search = False
min_indent = 0
for line in text.splitlines():
    stripped = line.lstrip(" ")
    indent = len(line) - len(stripped)
    if stripped.startswith("tool_search:"):
        in_tool_search = True
        min_indent = indent + 2
        continue
    if in_tool_search:
        if stripped and indent < min_indent and not stripped.startswith("#"):
            break
        if stripped.startswith("defer:"):
            raise SystemExit(0)
raise SystemExit(1)
PY
    then
      ok "${label} tools.tool_search.defer omits session_search"
    else
      warn "${label} tools.tool_search.defer unset (curated default defers session_search) — re-run hermes-*-bootstrap"
    fi
  }
  check_session_search_undeferred "data/hermes/profiles/browser/config.yaml" "browser"
  check_session_search_undeferred "data/hermes/config.yaml" "default"
  check_intent_ack_default() {
    local cfg="data/hermes/config.yaml"
    if [[ ! -f "$cfg" ]]; then
      warn "default config missing (${cfg}) — re-run hermes-api-bootstrap / hermes-browser-bootstrap"
      return 0
    fi
    if python3 - "$cfg" <<'PY'
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
in_agent = False
min_indent = 0
for line in text.splitlines():
    stripped = line.lstrip(" ")
    indent = len(line) - len(stripped)
    if stripped.startswith("agent:"):
        in_agent = True
        min_indent = indent + 2
        continue
    if not in_agent:
        continue
    if stripped and indent < min_indent and not stripped.startswith("#"):
        break
    if stripped.startswith("intent_ack_continuation:"):
        val = stripped.split(":", 1)[1].strip().strip("'\"")
        raise SystemExit(0 if val.lower() in ("true", "yes", "on", "always") else 1)
raise SystemExit(1)
PY
    then
      ok "default agent.intent_ack_continuation is on"
    else
      warn "default agent.intent_ack_continuation is off — re-run hermes-api-bootstrap / hermes-browser-bootstrap"
    fi
  }
  check_intent_ack_default
  if has_profile coding; then
    case "$(echo "$(env_get OPENCODE_MCP_ENABLED 0)" | tr '[:upper:]' '[:lower:]')" in
      1|true|yes)
        if [[ -f data/hermes/profiles/browser/config.yaml ]]; then
          if yaml_list_has "data/hermes/profiles/browser/config.yaml" \
            "tools.tool_search.always_include" "coding_start_task" \
            || yaml_list_has "data/hermes/profiles/browser/config.yaml" \
            "tools.tool_search.always_include" "mcp__opencode__coding_start_task"; then
            ok "browser always_include has OpenCode coding_* pins"
          else
            warn "browser always_include missing coding_* — re-run hermes-browser-bootstrap with OPENCODE_MCP_ENABLED=1"
          fi
        fi
        ;;
    esac
  fi
  if has_profile rag; then
    case "$(echo "$(env_get LIGHTRAG_MCP_ENABLED 0)" | tr '[:upper:]' '[:lower:]')" in
      1|true|yes)
        if [[ -f data/hermes/profiles/browser/config.yaml ]]; then
          _lr_missing=()
          for _lr_want in query_document get_documents; do
            if ! yaml_list_has "data/hermes/profiles/browser/config.yaml" \
              "tools.tool_search.always_include" "${_lr_want}" \
              && ! yaml_list_has "data/hermes/profiles/browser/config.yaml" \
              "tools.tool_search.always_include" "mcp__lightrag__${_lr_want}"; then
              _lr_missing+=("${_lr_want}")
            fi
          done
          if [[ ${#_lr_missing[@]} -eq 0 ]]; then
            ok "browser always_include has LightRAG query_document + get_documents pins"
          else
            warn "browser always_include missing LightRAG ${_lr_missing[*]} — re-run hermes-browser-bootstrap with LIGHTRAG_MCP_ENABLED=1"
          fi
          unset _lr_missing _lr_want
        fi
        ;;
    esac
  fi
  if docker compose version >/dev/null 2>&1 \
    && docker compose ps --status running hermes 2>/dev/null | grep -q hermes; then
    if docker compose exec -T hermes python3 - <<'PY'
import sys
sys.path.insert(0, "/opt/hermes")
from tools.tool_search import load_config_readonly
raise SystemExit(1 if "session_search" in load_config_readonly().effective_defer_tools else 0)
PY
    then
      ok "runtime tool_search does not defer session_search"
    else
      warn "runtime tool_search still defers session_search — recreate hermes after bootstrap"
    fi
    if docker compose exec -T hermes python3 - <<'PY'
import sys
sys.path.insert(0, "/opt/hermes")
from pathlib import Path
from tools.tool_search import is_deferrable_tool_name, load_config_readonly

text = Path("/opt/hermes/tools/tool_search.py").read_text()
if "assistant-stack: honor always_include" not in text:
    raise SystemExit(2)
cfg = load_config_readonly()
pins = getattr(cfg, "always_include", frozenset()) or frozenset()
want = "mcp__searxng__searxng_web_search"
if want not in pins:
    raise SystemExit(3)
raise SystemExit(1 if is_deferrable_tool_name(want, cfg.effective_defer_tools) else 0)
PY
    then
      ok "runtime always_include keeps SearXNG eager"
    else
      warn "runtime still defers SearXNG — recreate hermes after bootstrap (always_include overlay)"
    fi
  fi
fi

# --- Browser CLI ---
# agent-browser must live on the container filesystem. A 644 copy under
# data/hermes (virtiofs) fails exec with EACCES; reinstalling Chromium does not
# fix that. Cont-init 08 installs the CLI on container start.
if has_profile core \
  && docker compose version >/dev/null 2>&1 \
  && docker compose ps --status running hermes 2>/dev/null | grep -q hermes; then
  echo
  echo "--- Browser CLI ---"
  if docker compose exec -T -u hermes hermes agent-browser --version >/dev/null 2>&1; then
    ok "hermes user can exec agent-browser"
  else
    warn "agent-browser is not executable in the hermes container — docker compose restart hermes (do not reinstall Chromium)"
  fi
  # browser-harness control sockets must stay off virtiofs too. A socket left
  # under data/hermes across a Docker VM restart fails with EOPNOTSUPP on connect
  # and unlink, and then every browser_exec for that session fails.
  _bh_runtime="$(docker compose exec -T -u hermes hermes printenv BH_RUNTIME_DIR 2>/dev/null || true)"
  if [[ "$_bh_runtime" == /tmp/* ]]; then
    ok "browser-harness runtime dir is ${_bh_runtime} (container filesystem)"
    _bh_stale="$(find data/hermes/home/.config/browser-harness/runtime -maxdepth 1 -name '*.sock' 2>/dev/null || true)"
    if [[ -n "$_bh_stale" ]]; then
      warn "stale browser-harness sockets under data/hermes (unused since BH_RUNTIME_DIR moved): rm data/hermes/home/.config/browser-harness/runtime/*.sock data/hermes/home/.config/browser-harness/runtime/*.pid"
    fi
  else
    warn "BH_RUNTIME_DIR is not under /tmp in the hermes container — browser_exec breaks after a restart; docker compose up -d hermes"
  fi
  unset _bh_runtime _bh_stale
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
  check_port "$(env_get NEO4J_HTTP_HOST_PORT 7474)" "Neo4j browser"
fi
if has_profile automation; then
  check_port "$(env_get N8N_HOST_PORT 5678)" "n8n"
  check_port "$(env_get GPTR_PORT 8000)" "GPT Researcher"
fi
if has_profile coding; then
  check_port "$(env_get OPENCODE_PORT 4096)" "OpenCode"
  mcp_en="$(env_get OPENCODE_MCP_ENABLED 0)"
  case "$(echo "$mcp_en" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes) ok "OPENCODE_MCP_ENABLED is on (Hermes coding_* delegation)" ;;
    *) warn "coding profile on but OPENCODE_MCP_ENABLED is not 1 — Hermes will not register coding_* tools" ;;
  esac
  if docker compose ps --status running --services 2>/dev/null | grep -qx opencode-mcp; then
    ok "opencode-mcp container is running"
  else
    warn "opencode-mcp is not running (start with coding profile: docker compose up -d opencode-mcp)"
  fi
fi
if has_profile ollama; then
  check_port "$(env_get OLLAMA_HOST_PORT 11434)" "Ollama"
fi

# --- Signal (optional; toggled by HERMES_SIGNAL_ENABLED) ---
signal_enabled="$(env_get HERMES_SIGNAL_ENABLED 0)"
case "$(echo "$signal_enabled" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes) signal_enabled=1 ;;
  *) signal_enabled=0 ;;
esac

if [[ "$signal_enabled" -eq 1 ]]; then
  echo
  echo "--- Signal ---"
  account="$(env_get SIGNAL_ACCOUNT)"
  if [[ -n "$account" ]]; then
    ok "SIGNAL_ACCOUNT is set"
  else
    bad "HERMES_SIGNAL_ENABLED=1 but SIGNAL_ACCOUNT is empty"
  fi
  data_dir="$(env_get SIGNAL_CLI_DATA_DIR "${HOME}/.local/share/signal-cli")"
  data_dir="${data_dir/\$HOME/$HOME}"
  data_dir="${data_dir/#\~/$HOME}"
  if [[ -d "$data_dir" ]]; then
    ok "SIGNAL_CLI_DATA_DIR exists (${data_dir})"
  else
    bad "SIGNAL_CLI_DATA_DIR missing (${data_dir}) — link signal-cli on the host first"
  fi
  if has_profile signal; then
    ok "COMPOSE_PROFILES includes signal"
  else
    bad "HERMES_SIGNAL_ENABLED=1 but 'signal' not in COMPOSE_PROFILES — run make ensure-local"
  fi
  if [[ -f data/hermes/.env ]] && grep -q '^SIGNAL_HTTP_URL=http://signal-cli:8080' data/hermes/.env; then
    ok "data/hermes/.env has SIGNAL_HTTP_URL=http://signal-cli:8080"
  else
    warn "data/hermes/.env missing Signal adapter URL — run make ensure-local"
  fi
  home_ch="$(env_get SIGNAL_HOME_CHANNEL)"
  if [[ "$home_ch" == *"="* ]]; then
    bad "SIGNAL_HOME_CHANNEL is corrupted (contains '=' / mashed next key) — fix the newline in .env"
  elif [[ -n "$home_ch" ]]; then
    ok "SIGNAL_HOME_CHANNEL is set (cron + WebUI default delivery target)"
  else
    bad "SIGNAL_HOME_CHANNEL is empty — set it for cron/WebUI delivery (e.g. Note to Self number)"
  fi
  browser_cfg="data/hermes/profiles/browser/config.yaml"
  if [[ -f "$browser_cfg" ]]; then
    if grep -A3 '^  signal:' "$browser_cfg" 2>/dev/null | grep -q 'enabled: false'; then
      ok "browser profile has platforms.signal.enabled=false (default owns SSE)"
    else
      warn "browser config missing platforms.signal.enabled=false — run make ensure-local or recreate bootstrap"
    fi
  fi
  if [[ -f data/hermes/profiles/browser/.env ]] && grep -q '^SIGNAL_HTTP_URL=http://signal-cli:8080' data/hermes/profiles/browser/.env; then
    ok "browser profile has Signal credentials for WebUI send"
  else
    warn "browser profile missing Signal credentials — run make ensure-local"
  fi
  browser_jobs="data/hermes/profiles/browser/cron/jobs.json"
  if [[ -f "$browser_jobs" ]] && grep -q 'last_delivery_error' "$browser_jobs" \
    && grep -qE 'api_server|not send\(\)' "$browser_jobs" 2>/dev/null; then
    warn "browser cron jobs have api_server delivery errors — recreate hermes and re-run jobs"
  fi
  if docker info >/dev/null 2>&1; then
    if docker compose ps --status running hermes 2>/dev/null | grep -q hermes; then
      if docker compose exec -T hermes grep -q 'assistant-stack: reject undeliverable cron origin' /opt/hermes/cron/scheduler_delivery.py 2>/dev/null \
        || docker compose exec -T hermes grep -q 'assistant-stack: reject undeliverable cron origin' /opt/hermes/cron/scheduler.py 2>/dev/null; then
        if docker compose exec -T hermes grep -q 'assistant-stack: cron synthesize Signal' /opt/hermes/cron/scheduler.py 2>/dev/null \
          || docker compose exec -T hermes grep -q 'assistant-stack: cron synthesize Signal' /opt/hermes/cron/scheduler_delivery.py 2>/dev/null; then
          if docker compose exec -T hermes grep -q 'assistant-stack: default WebUI cron deliver to signal' /opt/hermes/tools/cronjob_tools.py 2>/dev/null; then
            ok "Hermes cron Signal patches applied"
          else
            warn "Hermes cron Signal patches missing — docker compose up -d --force-recreate hermes"
          fi
        else
          warn "Hermes cron Signal patches missing — docker compose up -d --force-recreate hermes"
        fi
      else
        warn "Hermes cron Signal patches missing — docker compose up -d --force-recreate hermes"
      fi
    fi
    if docker compose ps --status running signal-cli 2>/dev/null | grep -q signal-cli; then
      if docker compose exec -T hermes curl -sf --max-time 5 http://signal-cli:8080/api/v1/check >/dev/null 2>&1; then
        ok "signal-cli healthy (reachable from hermes)"
      elif docker compose exec -T signal-cli bash -c 'exec 3<>/dev/tcp/127.0.0.1/8080' >/dev/null 2>&1; then
        ok "signal-cli port 8080 is open"
      else
        warn "signal-cli container is up but HTTP check failed"
      fi
    else
      warn "signal-cli not running — make up after ensure-local"
    fi
  fi
fi

# --- Buzz (optional; toggled by HERMES_BUZZ_ENABLED) ---
buzz_enabled="$(env_get HERMES_BUZZ_ENABLED 0)"
case "$(echo "$buzz_enabled" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes) buzz_enabled=1 ;;
  *) buzz_enabled=0 ;;
esac

# --- Independent agents registry (optional) ---
if [[ -f data/hermes/agents/registry.json ]]; then
  echo
  echo "--- Independent agents ---"
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    if [[ -d "data/hermes/profiles/${name}" ]]; then
      ok "agent '${name}' profile exists"
    else
      bad "registry lists '${name}' but data/hermes/profiles/${name} is missing"
    fi
  done < <(
    python3 - <<'PY' 2>/dev/null || true
import json
from pathlib import Path
path = Path("data/hermes/agents/registry.json")
try:
    data = json.loads(path.read_text())
except Exception:
    raise SystemExit(0)
for a in data.get("agents", []):
    if isinstance(a, dict) and a.get("name"):
        print(a["name"])
PY
  )
fi

if [[ "$buzz_enabled" -eq 1 ]]; then
  echo
  echo "--- Buzz ---"
  relay="$(env_get BUZZ_RELAY_URL)"
  if [[ -n "$relay" ]]; then
    ok "BUZZ_RELAY_URL is set"
  else
    warn "BUZZ_RELAY_URL is empty — set it in .env"
  fi
  local_buzz="$(env_get BUZZ_LOCAL_DIR_PATH)"
  if [[ -n "$local_buzz" ]]; then
    expanded_local="$local_buzz"
    if [[ "$expanded_local" == "~" ]]; then
      expanded_local="$HOME"
    elif [[ "$expanded_local" == "~/"* ]]; then
      expanded_local="$HOME/${expanded_local#~/}"
    fi
    if [[ -d "$expanded_local" ]]; then
      ok "BUZZ_LOCAL_DIR_PATH is set"
    else
      warn "BUZZ_LOCAL_DIR_PATH is set but path missing — make bootstrap-buzz or fix .env"
    fi
    if ./scripts/buzz-relay.sh status 2>/dev/null | grep -q 'running'; then
      ok "local Buzz relay is running (make buzz-relay-status)"
    else
      warn "local Buzz relay not running — make buzz-relay-start"
    fi
  else
    warn "BUZZ_LOCAL_DIR_PATH unset — make bootstrap-buzz for a local relay (or use a hosted BUZZ_RELAY_URL)"
  fi
  if [[ -x data/hermes/.local/bin/buzz ]]; then
    ok "Buzz CLI present at data/hermes/.local/bin/buzz"
  else
    bad "Buzz CLI missing — run make buzz-cli-install (or make bootstrap-buzz)"
  fi
  profiles_csv="$(env_get HERMES_BUZZ_PROFILES)"
  profiles_csv="${profiles_csv// /}"
  if [[ -z "$profiles_csv" ]]; then
    warn "HERMES_BUZZ_PROFILES is empty — make agent-create NAME=<slug> WITH=buzz"
  else
    ok "HERMES_BUZZ_PROFILES=${profiles_csv}"
    IFS=',' read -r -a buzz_profiles <<< "$profiles_csv"
    for name in "${buzz_profiles[@]}"; do
      [[ -n "$name" ]] || continue
      if [[ -d "data/hermes/profiles/${name}" ]]; then
        ok "Buzz agent directory exists: data/hermes/profiles/${name}"
        if [[ -f "data/hermes/profiles/${name}/.env" ]] \
          && grep -q '^BUZZ_PRIVATE_KEY=' "data/hermes/profiles/${name}/.env" \
          && ! grep -qE '^BUZZ_PRIVATE_KEY=(CHANGE_ME)?$' "data/hermes/profiles/${name}/.env"; then
          ok "profile ${name} has BUZZ_PRIVATE_KEY set"
        else
          warn "profile ${name} needs a real BUZZ_PRIVATE_KEY — re-run: make agent-create NAME=${name} WITH=buzz"
        fi
        if [[ -f data/hermes/agents/registry.json ]]; then
          if NAME="$name" python3 - <<'PY' 2>/dev/null
import json, os, sys
from pathlib import Path
name = os.environ["NAME"]
path = Path("data/hermes/agents/registry.json")
try:
    data = json.loads(path.read_text())
except Exception:
    sys.exit(1)
for a in data.get("agents", []):
    if isinstance(a, dict) and a.get("name") == name:
        sys.exit(0)
sys.exit(1)
PY
          then
            ok "agent ${name} is in data/hermes/agents/registry.json"
          else
            warn "Buzz profile ${name} not in agent registry — make agent-create NAME=${name} (or ignore if legacy)"
          fi
        fi
      else
        bad "Buzz agent '${name}' missing — make agent-create NAME=${name} WITH=buzz"
      fi
    done
  fi
  for reserved in browser api-server; do
    cfg="data/hermes/profiles/${reserved}/config.yaml"
    if [[ -f "$cfg" ]]; then
      if grep -A5 'buzz:' "$cfg" 2>/dev/null | grep -q 'enabled: false'; then
        ok "${reserved} profile has buzz disabled"
      else
        warn "${reserved} may enable Buzz — run make ensure-local"
      fi
    fi
  done
  if docker info >/dev/null 2>&1 \
    && docker compose ps --status running hermes 2>/dev/null | grep -q hermes; then
    if docker compose exec -T hermes buzz --help >/dev/null 2>&1; then
      ok "hermes container can exec buzz"
    else
      warn "hermes cannot exec buzz — check make buzz-cli-install / arch match"
    fi
  fi
fi

# --- CalDAV calendar ---
caldav_enabled="$(env_get CALDAV_MCP_ENABLED 0)"
if [[ "$caldav_enabled" == "1" || "$caldav_enabled" == "true" || "$caldav_enabled" == "TRUE" || "$caldav_enabled" == "yes" ]] \
  || has_profile calendar; then
  echo
  echo "--- CalDAV calendar ---"
  if has_profile calendar; then
    ok "COMPOSE_PROFILES includes calendar"
  else
    bad "CALDAV_MCP_ENABLED=1 but 'calendar' not in COMPOSE_PROFILES — run ./scripts/setup.sh --calendar"
  fi
  if [[ "$caldav_enabled" == "1" || "$caldav_enabled" == "true" || "$caldav_enabled" == "TRUE" || "$caldav_enabled" == "yes" ]]; then
    ok "CALDAV_MCP_ENABLED=1"
  else
    warn "calendar profile on but CALDAV_MCP_ENABLED is not 1 — Hermes will skip CalDAV MCP registration"
  fi
  accounts_dir="compose/caldav-mcp/accounts"
  valid_accounts=0
  caldav_ready_slugs=()
  if [[ -d "$accounts_dir" ]]; then
    for acc in "$accounts_dir"/*.env; do
      [[ -f "$acc" ]] || continue
      base="$(basename "$acc")"
      slug="${base%.env}"
      url="$(grep -E '^CALDAV_URL=' "$acc" 2>/dev/null | head -1 | cut -d= -f2- || true)"
      user="$(grep -E '^CALDAV_USERNAME=' "$acc" 2>/dev/null | head -1 | cut -d= -f2- || true)"
      pass="$(grep -E '^CALDAV_PASSWORD=' "$acc" 2>/dev/null | head -1 | cut -d= -f2- || true)"
      if [[ -z "$url" || -z "$user" || -z "$pass" ]]; then
        warn "CalDAV account ${slug}: incomplete CALDAV_URL/USERNAME/PASSWORD"
        continue
      fi
      if [[ "$pass" == "xxxx-xxxx-xxxx-xxxx" || "$user" == "you@icloud.com" ]]; then
        warn "CalDAV account ${slug}: still has placeholder credentials — edit ${acc}"
        continue
      fi
      ok "CalDAV account ${slug} looks filled in"
      valid_accounts=$((valid_accounts + 1))
      caldav_ready_slugs+=("$slug")
    done
  fi
  if [[ "$valid_accounts" -eq 0 ]]; then
    bad "No valid CalDAV accounts in ${accounts_dir}/ — copy account.env.example to accounts/<slug>.env"
  else
    ok "${valid_accounts} CalDAV account file(s) ready"
  fi
  if [[ ${#caldav_ready_slugs[@]} -gt 0 && -f compose/hermes/bootstrap-api-profile.sh ]]; then
    # shellcheck disable=SC1090
    eval "$(sed -n '/^caldav_password_env_key() {/,/^}/p' compose/hermes/bootstrap-api-profile.sh)"
    for slug in "${caldav_ready_slugs[@]}"; do
      key="$(caldav_password_env_key "$slug")"
      ref="\${${key}}"
      check_caldav_profile_ref() {
        local label="$1" cfg="$2" envfile="$3" header="" rc=0
        [[ -f "$cfg" ]] || return 0
        header="$(python3 - "$cfg" "$slug" <<'PY'
import sys
path, slug = sys.argv[1], sys.argv[2]
target = f"caldav-{slug}"
in_server = False
server_indent = None
try:
    lines = open(path, encoding="utf-8").read().splitlines()
except OSError:
    sys.exit(2)
for line in lines:
    stripped = line.lstrip(" ")
    if not in_server:
        if stripped.startswith(target + ":") and stripped.split(":", 1)[0].strip() == target:
            in_server = True
            server_indent = len(line) - len(stripped)
        continue
    if not line.strip():
        continue
    indent = len(line) - len(line.lstrip(" "))
    if indent <= server_indent:
        break
    body = line.strip()
    prefix = "X-Caldav-Password:"
    if body.startswith(prefix):
        value = body[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        sys.stdout.write(value)
        sys.exit(0)
sys.exit(3)
PY
)" || rc=$?
        case "$rc" in
          0)
            if [[ "$header" == "$ref" ]]; then
              if [[ -f "$envfile" ]] && grep -q "^${key}=" "$envfile"; then
                ok "caldav-${slug} password on ${label} is a profile env reference"
              else
                bad "caldav-${slug} header references ${key} but ${envfile} does not set it"
              fi
            else
              warn "caldav-${slug} X-Caldav-Password in ${cfg} is not \${${key}} — re-run hermes-api-bootstrap and hermes-browser-bootstrap"
            fi
            ;;
          2) ;;
          3)
            warn "caldav-${slug} in ${cfg} has no X-Caldav-Password reference — re-run hermes-api-bootstrap and hermes-browser-bootstrap"
            ;;
          *)
            warn "could not read CalDAV headers in ${cfg}"
            ;;
        esac
      }
      check_caldav_profile_ref default \
        data/hermes/config.yaml \
        data/hermes/.env
      check_caldav_profile_ref api-server \
        data/hermes/profiles/api-server/config.yaml \
        data/hermes/profiles/api-server/.env
      check_caldav_profile_ref browser \
        data/hermes/profiles/browser/config.yaml \
        data/hermes/profiles/browser/.env
    done
  fi
  if docker info >/dev/null 2>&1; then
    if docker compose ps --status running caldav-mcp 2>/dev/null | grep -q caldav-mcp; then
      ok "caldav-mcp container is running"
    else
      warn "caldav-mcp not running — make up with calendar in COMPOSE_PROFILES"
    fi
    if docker compose ps --status running hermes 2>/dev/null | grep -q hermes; then
      for acc in "$accounts_dir"/*.env; do
        [[ -f "$acc" ]] || continue
        slug="$(basename "$acc" .env)"
        pass="$(grep -E '^CALDAV_PASSWORD=' "$acc" 2>/dev/null | head -1 | cut -d= -f2- || true)"
        user="$(grep -E '^CALDAV_USERNAME=' "$acc" 2>/dev/null | head -1 | cut -d= -f2- || true)"
        [[ "$pass" == "xxxx-xxxx-xxxx-xxxx" || "$user" == "you@icloud.com" ]] && continue
        if docker compose exec -T hermes hermes mcp list 2>/dev/null | grep -q "caldav-${slug}"; then
          ok "Hermes default profile has mcp caldav-${slug}"
        else
          warn "Hermes missing caldav-${slug} — re-run hermes-api-bootstrap after filling credentials"
        fi
      done
      if docker compose exec -T hermes hermes -p api-server mcp list 2>/dev/null | grep -q 'caldav-'; then
        ok "api-server profile has at least one caldav-* MCP entry"
      else
        warn "api-server has no caldav-* MCP — re-run hermes-api-bootstrap"
      fi
    fi
  fi
fi

# --- Knowledge corpus (rag) ---
if has_profile rag; then
  echo
  echo "--- Knowledge corpus ---"
  WS="$(env_get WORKSPACE)"
  DATA_ROOT="$(env_get ASSISTANT_DATA_ROOT ./data)"
  # Resolve relative to repo root for existence checks
  if [[ "$DATA_ROOT" != /* ]]; then
    DATA_ROOT="${ROOT}/${DATA_ROOT#./}"
  fi
  if [[ -n "$WS" ]]; then
    ok "active WORKSPACE=${WS}"
    if [[ -d "${DATA_ROOT}/inputs/${WS}" ]]; then
      ok "${DATA_ROOT}/inputs/${WS}/ exists"
    else
      warn "${DATA_ROOT}/inputs/${WS}/ missing — run: make corpus-create SLUG=${WS}"
    fi
  else
    warn "WORKSPACE unset in .env"
  fi
  if [[ -n "$(env_get NEO4J_WORKSPACE)" ]]; then
    bad "NEO4J_WORKSPACE is set — unset it or every corpus collapses into one Neo4j label"
  else
    ok "NEO4J_WORKSPACE is unset (correct for multi-corpus)"
  fi
  if [[ -n "$(env_get POSTGRES_WORKSPACE)" ]]; then
    bad "POSTGRES_WORKSPACE is set — unset it or workspace isolation collapses"
  fi
  REG="${DATA_ROOT}/corpora/registry.json"
  if [[ -f "$REG" && -n "$WS" ]]; then
    REG_DIM="$(python3 - "$REG" "$WS" <<'PY'
import json, sys
from pathlib import Path
path, slug = Path(sys.argv[1]), sys.argv[2]
data = json.loads(path.read_text())
for c in data.get("corpora", []):
    if c.get("slug") == slug:
        print(c.get("embedding_dim") or "")
        break
PY
)"
    CUR_DIM="$(env_get EMBEDDING_DIM)"
    if [[ -n "$REG_DIM" && -n "$CUR_DIM" && "$REG_DIM" != "$CUR_DIM" ]]; then
      bad "active corpus dim=${REG_DIM} but EMBEDDING_DIM=${CUR_DIM} — mismatch corrupts vectors"
    elif [[ -n "$REG_DIM" ]]; then
      ok "registry embedding_dim=${REG_DIM} matches EMBEDDING_DIM for ${WS}"
    fi
  fi
  if docker info >/dev/null 2>&1; then
    LR_PORT="$(env_get PORT 9621)"
    LR_KEY="$(env_get LIGHTRAG_API_KEY)"
    if curl -sf -H "X-API-Key: ${LR_KEY}" "http://127.0.0.1:${LR_PORT}/health" >/dev/null 2>&1; then
      ok "LightRAG health OK on :${LR_PORT}"
    else
      warn "LightRAG not healthy on :${LR_PORT} (start with make up, or switch: make corpus-use SLUG=...)"
    fi
  fi
fi

# --- Compose config ---
if [[ -f .env ]] && docker compose version >/dev/null 2>&1; then
  if docker compose config >/dev/null 2>&1; then
    ok "docker compose config validates"
  else
    bad "docker compose config failed — check .env and local overlay files"
  fi
  if python3 scripts/test-compose-env-allowlist.py; then
    ok "compose service env allowlists"
  else
    bad "compose service env allowlists"
  fi
  if python3 scripts/test-terminal-backend-compose.py; then
    ok "terminal backend compose layouts"
  else
    bad "terminal backend compose layouts"
  fi
fi

terminal_backend="$(env_get HERMES_TERMINAL_BACKEND ssh)"
case "$terminal_backend" in
  ssh|local)
    ok "HERMES_TERMINAL_BACKEND=${terminal_backend}"
    ;;
  *)
    bad "HERMES_TERMINAL_BACKEND must be ssh or local (got ${terminal_backend})"
    terminal_backend=""
    ;;
esac

if [[ "$terminal_backend" == "ssh" ]]; then
  if [[ -f compose/hermes/worker/ssh/id_ed25519 && -f compose/hermes/worker/ssh/id_ed25519.pub ]]; then
    ok "hermes-worker SSH key present"
  else
    warn "hermes-worker SSH key missing — run ./scripts/setup.sh"
  fi
elif [[ "$terminal_backend" == "local" ]]; then
  ok "local terminal backend does not use the worker key"
fi

# Profile config.yaml must match the env var or the shell runs in the wrong place.
if [[ -n "$terminal_backend" ]]; then
  backend_mismatch="$(python3 - "$terminal_backend" <<'PY'
import sys
from pathlib import Path

want = sys.argv[1]
paths = [Path("data/hermes/config.yaml")]
profiles = Path("data/hermes/profiles")
if profiles.is_dir():
    paths.extend(sorted(profiles.glob("*/config.yaml")))

def backend(path: Path):
    in_terminal = False
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("terminal:"):
            in_terminal = True
            continue
        if in_terminal:
            if line and not line.startswith(" "):
                break
            stripped = line.strip()
            if stripped.startswith("backend:"):
                return stripped.split(":", 1)[1].strip().strip("'\"")
    return ""

bad = []
saw = False
for path in paths:
    if not path.is_file():
        continue
    saw = True
    got = backend(path)
    if got != want:
        label = "default" if path.parent.name == "hermes" else path.parent.name
        bad.append(f"{label} terminal.backend is {got or 'unset'}")
if not saw:
    print("NOCONFIG")
else:
    print("\n".join(bad))
PY
)"
  if [[ "$backend_mismatch" == "NOCONFIG" ]]; then
    :
  elif [[ -n "$backend_mismatch" ]]; then
    warn "Hermes terminal backend does not match HERMES_TERMINAL_BACKEND=${terminal_backend}"
    warn "  re-run hermes-api-bootstrap and hermes-browser-bootstrap, then recreate hermes"
    while IFS= read -r line; do
      [[ -n "$line" ]] && warn "  ${line}"
    done <<< "$backend_mismatch"
  else
    ok "Hermes profiles match HERMES_TERMINAL_BACKEND=${terminal_backend}"
  fi
fi

# ssh: extra host binds on hermes undo the secret split.
# local: those binds belong on hermes; a leftover hermes-worker block breaks compose.
if [[ -f docker-compose.override.yml && -n "$terminal_backend" ]]; then
  if [[ "$terminal_backend" == "ssh" ]]; then
    leftover="$(python3 - docker-compose.override.yml <<'PY'
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text()
lines = text.splitlines()
in_hermes = False
found = []
for line in lines:
    stripped = line.strip()
    if line.startswith("  ") and not line.startswith("    ") and stripped.endswith(":"):
        in_hermes = stripped == "hermes:"
        continue
    if not in_hermes or stripped.startswith("#"):
        continue
    if not stripped.startswith("- "):
        continue
    spec = stripped[2:].strip().strip("'\"")
    src = spec.split(":", 1)[0]
    if src.startswith("/"):
        found.append(src)
print("\n".join(found))
PY
)"
    if [[ -n "$leftover" ]]; then
      warn "docker-compose.override.yml still mounts a host path on hermes; run make ensure-local"
      while IFS= read -r path; do
        [[ -n "$path" ]] && warn "  leftover hermes mount: $path"
      done <<< "$leftover"
    else
      ok "no leftover host-path mounts on hermes"
    fi
  else
    worker_mounts="$(python3 - docker-compose.override.yml <<'PY'
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text()
lines = text.splitlines()
in_worker = False
found = []
for line in lines:
    stripped = line.strip()
    if line.startswith("  ") and not line.startswith("    ") and stripped.endswith(":"):
        in_worker = stripped == "hermes-worker:"
        continue
    if not in_worker or stripped.startswith("#"):
        continue
    if stripped.startswith("- "):
        found.append(stripped[2:].strip().strip("'\""))
print("\n".join(found))
PY
)"
    if [[ -n "$worker_mounts" ]]; then
      warn "docker-compose.override.yml still mounts paths on hermes-worker; run make ensure-local"
      while IFS= read -r path; do
        [[ -n "$path" ]] && warn "  leftover hermes-worker mount: $path"
      done <<< "$worker_mounts"
    else
      ok "no leftover hermes-worker mounts in local mode"
    fi
  fi
fi

# Clients must call llm-proxy. A profile still pointing at the model server
# keeps the real key in Hermes config or OpenCode's local file.
PROXY_URL="http://llm-proxy:4000/v1"
proxy_mismatch="$(python3 - "$PROXY_URL" <<'PY'
import json
import sys
from pathlib import Path

want = sys.argv[1]
bad = []

def base_url(path: Path):
    in_model = False
    for line in path.read_text(errors="replace").splitlines():
        stripped = line.strip()
        if line.startswith("model:") or stripped == "model:":
            in_model = True
            continue
        if in_model and line and not line.startswith(" ") and not line.startswith("\t"):
            in_model = False
        if not in_model or not stripped.startswith("base_url:"):
            continue
        value = stripped.split(":", 1)[1].strip().strip("'\"")
        return value
    return None

configs = [Path("data/hermes/config.yaml")]
profiles = Path("data/hermes/profiles")
if profiles.is_dir():
    configs.extend(sorted(profiles.glob("*/config.yaml")))
for cfg in configs:
    if not cfg.is_file():
        continue
    found = base_url(cfg)
    if found != want:
        bad.append(f"hermes {cfg}")

local = Path("opencode/opencode.local.json")
if local.is_file():
    try:
        data = json.loads(local.read_text())
    except json.JSONDecodeError:
        bad.append("opencode/opencode.local.json")
    else:
        for name, block in (data.get("provider") or {}).items():
            options = block.get("options") if isinstance(block, dict) else None
            url = options.get("baseURL") if isinstance(options, dict) else None
            if url != want:
                bad.append(f"opencode provider {name}")
print("\n".join(bad))
PY
)"
if [[ -n "$proxy_mismatch" ]]; then
  warn "a model client is not pointed at ${PROXY_URL}"
  while IFS= read -r item; do
    [[ -n "$item" ]] && warn "  ${item}"
  done <<< "$proxy_mismatch"
else
  ok "model clients point at llm-proxy"
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
    warn "EMBEDDING_DIM is unset — a mismatch corrupts the vector store with no error"
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
