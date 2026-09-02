#!/usr/bin/env bash
# Point the stack at a chat model (LM Studio, Ollama, etc.) in one shot:
# .env, Hermes profiles, and optionally OpenCode.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "$ROOT/scripts/lib/env.sh"

MODEL=""
FROM_LMSTUDIO=0
LIGHTRAG_MODE=""
RESTART=0
EXPLICIT_PRESET=0
NON_INTERACTIVE=0

usage() {
  cat <<'EOF'
Usage: ./scripts/model-use.sh [options] [MODEL_ID | PRESET]

Switch the stack's chat model across .env and Hermes (and OpenCode when the
coding profile is enabled). Load the model on your host server first (e.g. LM
Studio), then run this script.

Options:
  --from-lmstudio     List models from the host LM Studio /v1/models endpoint
                      and prompt to choose (default when a TTY is available)
  --yes, -y           With --from-lmstudio: skip the menu and pick the first
                      non-embedding model
  --non-interactive   Same as --yes
  --preset NAME       Resolve NAME from models.local.yaml or models.yaml
  --lightrag same     Also set LIGHTRAG_LLM_MODEL to the chat model (useful when
                      LM Studio serves one model at a time)
  --restart           Recreate compose services and restart Hermes
  -h, --help          Show this help

Examples:
  ./scripts/model-use.sh lmstudio-community/muse-glimmer-30b
  ./scripts/model-use.sh --from-lmstudio --lightrag same --restart
  ./scripts/model-use.sh --from-lmstudio -y   # auto-pick first chat model
  ./scripts/model-use.sh muse-glimmer         # preset from models.local.yaml
  make model-use FROM_LMSTUDIO=1 RESTART=1

Presets: copy models.yaml.example to models.local.yaml and add your aliases.
EOF
}

log() { printf '[model-use] %s\n' "$1"; }
die() { printf '[model-use] ERROR: %s\n' "$1" >&2; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

host_url_from_env() {
  local url="$1"
  url="${url//host.docker.internal/127.0.0.1}"
  printf '%s' "$url"
}

lmstudio_models_url() {
  local host_url base
  host_url="$(env_get LLM_BINDING_HOST http://host.docker.internal:1234/v1)"
  base="$(host_url_from_env "$host_url")"
  base="${base%/}"
  if [[ "$base" == */v1 ]]; then
    printf '%s/models' "$base"
  else
    printf '%s/v1/models' "$base"
  fi
}

using_lmstudio() {
  [[ "$(env_get LLM_BINDING openai)" == openai ]] || return 1
  local host
  host="$(env_get LLM_BINDING_HOST http://host.docker.internal:1234/v1)"
  [[ "$host" == *":1234"* ]]
}

resolve_preset() {
  local name="$1"
  local file line val
  for file in models.local.yaml models.yaml; do
    [[ -f "$file" ]] || continue
    line="$(grep -E "^${name}:[[:space:]]*" "$file" 2>/dev/null | head -1 || true)"
    [[ -n "$line" ]] || continue
    val="${line#*:}"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    printf '%s' "$val"
    return 0
  done
  return 1
}

pick_lmstudio_model() {
  local models_url current bash_interactive=0 picker_mode picker_script
  models_url="$(lmstudio_models_url)"
  current="$(env_get LLM_MODEL)"
  picker_script="$ROOT/scripts/lmstudio-picker.py"

  if [[ "$NON_INTERACTIVE" -eq 0 && -t 0 && "${MODEL_USE_NON_INTERACTIVE:-}" != "1" ]]; then
    bash_interactive=1
  fi
  if [[ "$bash_interactive" -eq 1 ]]; then
    picker_mode="interactive"
  else
    picker_mode="auto"
  fi

  if [[ "$picker_mode" == "interactive" ]]; then
    MODEL="$(python3 "$picker_script" "$models_url" "$current" interactive)" \
      || die "LM Studio model selection failed"
  else
    MODEL="$(python3 "$picker_script" "$models_url" "$current" auto)" \
      || die "LM Studio model selection failed"
  fi
}

hermes_runner() {
  if docker compose ps hermes --status running -q 2>/dev/null | grep -q .; then
    docker compose exec -T hermes hermes "$@"
  else
    docker compose run --rm --no-deps -T hermes hermes "$@"
  fi
}

hermes_profile_config_path() {
  local profile="$1"
  if [[ -z "$profile" ]]; then
    printf '%s' "data/hermes/config.yaml"
  else
    printf '%s' "data/hermes/profiles/${profile}/config.yaml"
  fi
}

update_hermes_profile() {
  local profile="$1"
  local label="$2"
  local cfg
  cfg="$(hermes_profile_config_path "$profile")"
  if [[ ! -f "$cfg" ]]; then
    log "Skipping Hermes profile '${label}' (no ${cfg})"
    return 0
  fi

  local args=()
  [[ -n "$profile" ]] && args=(-p "$profile")

  log "Hermes ${label}: default=${MODEL} model=${MODEL}"
  hermes_runner "${args[@]}" config set model.provider custom
  hermes_runner "${args[@]}" config set model.default "$MODEL"
  hermes_runner "${args[@]}" config set model.model "$MODEL"
  hermes_runner "${args[@]}" config set model.base_url "$BASE_URL"
  hermes_runner "${args[@]}" config set model.api_key "$API_KEY"
}

update_opencode() {
  local path="opencode/opencode.local.json"
  if ! has_compose_profile coding; then
    return 0
  fi
  if [[ ! -f "$path" ]]; then
    log "Skipping OpenCode (${path} missing; run make ensure-local)"
    return 0
  fi

  python3 - "$path" "$MODEL" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
model_id = sys.argv[2]
cfg = json.loads(path.read_text())

current = cfg.get("model", "")
provider = current.split("/", 1)[0] if "/" in current else ""
providers = cfg.get("provider") or {}
if not provider or provider not in providers:
    provider = next(iter(providers), "local")

cfg["model"] = f"{provider}/{model_id}"
block = providers.setdefault(provider, {})
models = block.setdefault("models", {})
if model_id not in models:
    short = model_id.rsplit("/", 1)[-1]
    models[model_id] = {"name": short}

path.write_text(json.dumps(cfg, indent=2) + "\n")
print(f"OpenCode: model={cfg['model']}")
PY
  log "OpenCode default model updated"
}

update_env_models() {
  local prev
  prev="$(env_get LLM_MODEL)"
  upsert_env .env LLM_MODEL "$MODEL"
  log ".env LLM_MODEL=${MODEL} (was: ${prev:-<unset>})"

  if [[ "$LIGHTRAG_MODE" == same ]]; then
    prev="$(env_get LIGHTRAG_LLM_MODEL)"
    upsert_env .env LIGHTRAG_LLM_MODEL "$MODEL"
    log ".env LIGHTRAG_LLM_MODEL=${MODEL} (was: ${prev:-<unset>})"
  fi

  # Keep explicit GPT Researcher overrides in sync when present.
  local gptr_prefix="openai:${MODEL}"
  if grep -q '^FAST_LLM=' .env 2>/dev/null; then
    upsert_env .env FAST_LLM "$gptr_prefix"
    upsert_env .env SMART_LLM "$gptr_prefix"
    upsert_env .env STRATEGIC_LLM "$gptr_prefix"
    log ".env FAST/SMART/STRATEGIC_LLM synced"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-lmstudio) FROM_LMSTUDIO=1; shift ;;
    --yes|-y|--non-interactive) NON_INTERACTIVE=1; shift ;;
    --preset)
      EXPLICIT_PRESET=1
      MODEL="$(resolve_preset "$2")" || die "Unknown preset: $2"
      shift 2
      ;;
    --lightrag)
      [[ "${2:-}" == same ]] || die "--lightrag requires 'same'"
      LIGHTRAG_MODE=same
      shift 2
      ;;
    --restart) RESTART=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    -*) die "Unknown option: $1 (try --help)" ;;
    *)
      if [[ -n "$MODEL" && "$EXPLICIT_PRESET" -eq 0 ]]; then
        die "Unexpected extra argument: $1"
      fi
      if [[ "$EXPLICIT_PRESET" -eq 1 ]]; then
        die "Unexpected argument after --preset: $1"
      fi
      # Bare word: preset name if listed, otherwise treat as model id.
      if [[ "$1" != */* ]] && preset_out="$(resolve_preset "$1" 2>/dev/null || true)" && [[ -n "$preset_out" ]]; then
        MODEL="$preset_out"
      else
        MODEL="$1"
      fi
      shift
      ;;
  esac
done

# No model id: offer LM Studio picker when that is the configured binding.
if [[ -z "$MODEL" && "$FROM_LMSTUDIO" -eq 0 ]] && using_lmstudio; then
  FROM_LMSTUDIO=1
fi

if [[ "$FROM_LMSTUDIO" -eq 1 ]]; then
  require_cmd python3
  pick_lmstudio_model
  log "Selected model: ${MODEL}"
fi

[[ -n "$MODEL" ]] || { usage; die "MODEL_ID, --from-lmstudio, or an LM Studio .env binding is required"; }
[[ -f .env ]] || die ".env not found — run ./scripts/setup.sh first"

require_cmd docker
require_cmd python3

BASE_URL="$(env_get LLM_BINDING_HOST http://host.docker.internal:1234/v1)"
API_KEY="$(env_get LLM_BINDING_API_KEY local-llm)"
# Hermes config set stores scalars verbatim; strip inline comments from .env values.
API_KEY="${API_KEY%%[[:space:]]#*}"

log "Switching stack to: ${MODEL}"
update_env_models
update_hermes_profile "" "default"
update_hermes_profile browser "browser"
update_hermes_profile api-server "api-server"
update_opencode

if [[ "$RESTART" -eq 1 ]]; then
  log "Recreating services (docker compose up -d)…"
  docker compose up -d
  if docker compose ps hermes -q 2>/dev/null | grep -q .; then
    log "Restarting Hermes…"
    docker compose restart hermes
  fi
fi

log "Done. Verify with: make doctor"
