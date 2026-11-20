#!/usr/bin/env bash
# Point the stack at a chat model (LM Studio, Ollama, etc.) in one shot.
# LLM_MODEL in .env is the stack's model; this writes it there and copies it
# to the places that cannot read .env: Hermes profiles and OpenCode.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "$ROOT/scripts/lib/env.sh"
# shellcheck source=lib/hermes-yaml.sh
source "$ROOT/scripts/lib/hermes-yaml.sh"

MODEL=""
FROM_LMSTUDIO=0
LIGHTRAG_MODE=""
RESTART=0
EXPLICIT_PRESET=0
NON_INTERACTIVE=0
SYNC=0

usage() {
  cat <<'EOF'
Usage: ./scripts/model-use.sh [options] [MODEL_ID | PRESET]
       ./scripts/model-use.sh --sync [--restart]

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
  --sync              Re-apply LLM_MODEL from .env to every Hermes profile and
                      OpenCode (after editing .env by hand or adding a profile)
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
  make model-sync

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

# Every Hermes profile in one container run. `hermes config set` costs about
# a second of CLI start-up per key, which added up to 15+ calls per switch.
# A profile whose model.base_url points somewhere other than llm-proxy has its
# own upstream and keeps its model.
update_hermes_profiles() {
  [[ -f data/hermes/config.yaml ]] || { log "Skipping Hermes (no data/hermes/config.yaml)"; return 0; }
  hermes_python "$MODEL" "$BASE_URL" "$API_KEY" <<'PY'
import contextlib
import io
import sys
from pathlib import Path

from hermes_cli.config import load_config, set_config_value
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

model, base_url, api_key = sys.argv[1:4]
root = Path("/opt/data")
homes = [root] + sorted(p.parent for p in root.glob("profiles/*/config.yaml"))
for home in homes:
    label = "default" if home == root else home.name
    token = set_hermes_home_override(home)
    try:
        current = (load_config().get("model") or {})
        current_url = current.get("base_url") if isinstance(current, dict) else None
        if current_url and current_url.rstrip("/") != base_url:
            print(f"[model-use] Hermes {label}: kept {current.get('default')} (base_url {current_url})")
            continue
        with contextlib.redirect_stdout(io.StringIO()):
            for key, value in (
                ("model.provider", "custom"),
                ("model.default", model),
                ("model.model", model),
                ("model.base_url", base_url),
                ("model.api_key", api_key),
            ):
                set_config_value(key, value)
        print(f"[model-use] Hermes {label}: {model}")
    finally:
        reset_hermes_home_override(token)
PY
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

proxy = "http://llm-proxy:4000/v1"
for block in providers.values():
    if not isinstance(block, dict):
        continue
    options = block.get("options")
    if not isinstance(options, dict):
        options = {}
        block["options"] = options
    options["baseURL"] = proxy
    options["apiKey"] = "local-llm"

path.write_text(json.dumps(cfg, indent=2) + "\n")
print(f"OpenCode: model={cfg['model']} baseURL={proxy}")
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
    --sync) SYNC=1; shift ;;
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

if [[ "$SYNC" -eq 1 ]]; then
  [[ -z "$MODEL" && "$FROM_LMSTUDIO" -eq 0 ]] || die "--sync takes no model; it uses LLM_MODEL from .env"
  MODEL="$(env_get LLM_MODEL)"
  [[ -n "$MODEL" ]] || die "--sync: LLM_MODEL is not set in .env"
fi

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

# Hermes and OpenCode talk to the proxy. The real host and key stay in .env
# for llm-proxy. Do not copy them into profile config.
BASE_URL="http://llm-proxy:4000/v1"
API_KEY="local-llm"

log "Switching stack to: ${MODEL}"
update_env_models
update_hermes_profiles
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
