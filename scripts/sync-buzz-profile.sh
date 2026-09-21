#!/usr/bin/env bash
# Sync Buzz settings into operator-owned Hermes profiles from HERMES_BUZZ_ENABLED.
# Profiles are never created here — they must already exist under data/hermes/profiles/.
# Called by make ensure-local and scripts/setup.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

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

# Upsert KEY=VALUE, keeping a single occurrence (first wins; later dupes dropped).
upsert_env() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp seen=0
  touch "$file"
  tmp="$(mktemp)"
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "${key}="* ]]; then
      if [[ "$seen" -eq 0 ]]; then
        printf '%s=%s\n' "$key" "$value"
        seen=1
      fi
    else
      printf '%s\n' "$line"
    fi
  done < "$file" > "$tmp"
  if [[ "$seen" -eq 0 ]]; then
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  mv "$tmp" "$file"
}

# s6 reconciler only auto-starts profiles whose gateway_state is "running".
mark_gateway_running() {
  local profile_name="$1"
  local state_file="data/hermes/profiles/${profile_name}/gateway_state.json"
  mkdir -p "data/hermes/profiles/${profile_name}"
  printf '{"gateway_state":"running"}\n' > "$state_file"
}

remove_env_key() {
  local file="$1"
  local key="$2"
  [[ -f "$file" ]] || return 0
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    local tmp
    tmp="$(mktemp)"
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" != "${key}="* ]]; then
        printf '%s\n' "$line"
      fi
    done < "$file" > "$tmp"
    mv "$tmp" "$file"
  fi
}

# Disable Buzz adapter on a stack profile that must not hold a Buzz identity lock.
disable_buzz_on_profile() {
  local profile_name="$1"
  local cfg="data/hermes/profiles/${profile_name}/config.yaml"
  [[ -f "$cfg" ]] || return 0

  if docker compose ps hermes --status running -q 2>/dev/null | grep -q .; then
    docker compose exec -T hermes python3 - "$profile_name" <<'PY'
import sys
from pathlib import Path
import yaml
name = sys.argv[1]
path = Path(f"/opt/data/profiles/{name}/config.yaml")
if not path.is_file():
    raise SystemExit(0)
data = yaml.safe_load(path.read_text()) or {}
platforms = data.setdefault("platforms", {})
buzz = platforms.setdefault("buzz", {})
if buzz.get("enabled") is not False:
    buzz["enabled"] = False
    platforms["buzz"] = buzz
    data["platforms"] = platforms
    path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    print(f"pinned platforms.buzz.enabled=false on {name}")
else:
    print(f"platforms.buzz.enabled already false on {name}")
PY
  elif ! grep -qF 'sync-buzz-profile.sh — stack profile' "$cfg"; then
    printf '\n# sync-buzz-profile.sh — stack profile must not own a Buzz identity.\nplatforms:\n  buzz:\n    enabled: false\n' >> "$cfg"
    log "Appended platforms.buzz.enabled=false to ${profile_name} config.yaml"
  fi
}

# Seed recommended Buzz gateway + display defaults for an agent profile.
enable_buzz_on_profile_config() {
  local profile_name="$1"
  local relay_url="$2"
  local cfg="data/hermes/profiles/${profile_name}/config.yaml"
  [[ -f "$cfg" ]] || return 0

  if docker compose ps hermes --status running -q 2>/dev/null | grep -q .; then
    docker compose exec -T hermes python3 - "$profile_name" "$relay_url" <<'PY'
import sys
from pathlib import Path
import yaml
name, relay = sys.argv[1], sys.argv[2]
path = Path(f"/opt/data/profiles/{name}/config.yaml")
if not path.is_file():
    raise SystemExit(0)
data = yaml.safe_load(path.read_text()) or {}

# gateway.platforms.buzz (Hermes canonical)
gateway = data.setdefault("gateway", {})
platforms = gateway.setdefault("platforms", {})
buzz = platforms.setdefault("buzz", {})
buzz["enabled"] = True
extra = buzz.setdefault("extra", {})
if relay:
    extra["relay_url"] = relay
extra.setdefault("require_mention", True)
# Local Buzz communities already ACL via relay membership. Hermes' separate
# allowlist (allow_all_users=false + empty BUZZ_ALLOWED_USERS) rejects the human
# with "Unauthorized user" after the eyes ack and never calls the LLM.
extra["allow_all_users"] = True
buzz["extra"] = extra
platforms["buzz"] = buzz
gateway["platforms"] = platforms
data["gateway"] = gateway

# Also pin top-level platforms.buzz.enabled for adapters that read it there.
top_platforms = data.setdefault("platforms", {})
top_buzz = top_platforms.setdefault("buzz", {})
top_buzz["enabled"] = True
top_platforms["buzz"] = top_buzz
data["platforms"] = top_platforms

display = data.setdefault("display", {})
disp_platforms = display.setdefault("platforms", {})
disp_buzz = disp_platforms.setdefault("buzz", {})
disp_buzz.setdefault("interim_assistant_messages", False)
disp_buzz.setdefault("tool_progress", "off")
disp_platforms["buzz"] = disp_buzz
display["platforms"] = disp_platforms
data["display"] = display

path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
print(f"enabled gateway.platforms.buzz on {name} allow_all_users=true")
PY
  else
    warn "hermes not running — seeded .env only for ${profile_name}; start hermes and re-run make ensure-local to write config.yaml defaults"
  fi
}

clear_seeded_buzz_env() {
  local hermes_env="$1"
  [[ -f "$hermes_env" ]] || return 0
  # Keep BUZZ_PRIVATE_KEY — operator secret; only clear shared relay seed if we manage disable.
  remove_env_key "$hermes_env" BUZZ_RELAY_URL
  remove_env_key "$hermes_env" BUZZ_ALLOW_ALL_USERS
  remove_env_key "$hermes_env" BUZZ_CLI_PATH
}

[[ -f .env ]] || die ".env missing — run ./scripts/setup.sh first"

enabled="$(env_get HERMES_BUZZ_ENABLED 0)"
case "$(echo "$enabled" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes) enabled=1 ;;
  *) enabled=0 ;;
esac

# Always keep stack gateways off Buzz (identity lock + WebUI/n8n roles).
disable_buzz_on_profile browser
disable_buzz_on_profile api-server

if [[ "$enabled" -eq 1 ]]; then
  relay="$(env_get BUZZ_RELAY_URL)"
  profiles_csv="$(env_get HERMES_BUZZ_PROFILES)"
  profiles_csv="${profiles_csv// /}"

  if [[ -z "$profiles_csv" ]]; then
    warn "HERMES_BUZZ_ENABLED=1 but HERMES_BUZZ_PROFILES is empty — create agents with: make agent-create NAME=<slug> WITH=buzz"
  fi

  if [[ -z "$relay" ]]; then
    warn "BUZZ_RELAY_URL is empty — set it in .env before expecting Buzz to connect"
  fi

  mkdir -p data/hermes/.local/bin
  if [[ ! -x data/hermes/.local/bin/buzz ]]; then
    warn "Buzz CLI missing at data/hermes/.local/bin/buzz — run: make buzz-cli-install"
  fi

  IFS=',' read -r -a buzz_profiles <<< "$profiles_csv"
  for name in "${buzz_profiles[@]}"; do
    [[ -n "$name" ]] || continue
    case "$name" in
      browser|api-server)
        warn "refusing to enable Buzz on reserved profile '${name}'"
        continue
        ;;
    esac
    profile_dir="data/hermes/profiles/${name}"
    if [[ ! -d "$profile_dir" ]]; then
      warn "Buzz profile '${name}' not found at ${profile_dir} — create with: make agent-create NAME=${name} WITH=buzz"
      continue
    fi
    if [[ -f data/hermes/agents/registry.json ]]; then
      if ! NAME="$name" python3 - <<'PY' 2>/dev/null
import json, os, sys
from pathlib import Path
name = os.environ["NAME"]
try:
    data = json.loads(Path("data/hermes/agents/registry.json").read_text())
except Exception:
    sys.exit(1)
for a in data.get("agents", []):
    if isinstance(a, dict) and a.get("name") == name:
        sys.exit(0)
sys.exit(1)
PY
      then
        warn "Buzz profile '${name}' is not in data/hermes/agents/registry.json — make agent-create NAME=${name} (does not auto-create)"
      fi
    fi
    mkdir -p "$profile_dir"
    touch "${profile_dir}/.env"
    if [[ -n "$relay" ]]; then
      # Keep loopback hostnames as-is: Buzz communities are keyed by URL host,
      # so ws://host.docker.internal would be a different community than Desktop's
      # ws://localhost. Reachability is handled by compose/hermes/06-buzz-localhost-proxy.
      upsert_env "${profile_dir}/.env" BUZZ_RELAY_URL "$relay"
    fi
    upsert_env "${profile_dir}/.env" BUZZ_CLI_PATH "/opt/data/.local/bin/buzz"
    # Mirror config: community membership is the ACL; Hermes allowlist must not
    # block the operator after the eyes reaction.
    upsert_env "${profile_dir}/.env" BUZZ_ALLOW_ALL_USERS "true"
    # Do not overwrite an existing private key.
    if ! grep -q '^BUZZ_PRIVATE_KEY=' "${profile_dir}/.env" 2>/dev/null; then
      upsert_env "${profile_dir}/.env" BUZZ_PRIVATE_KEY "CHANGE_ME"
      log "Seeded BUZZ_PRIVATE_KEY=CHANGE_ME in ${profile_dir}/.env — replace with the agent nsec"
    fi
    enable_buzz_on_profile_config "$name" "$relay"
    mark_gateway_running "$name"
    log "Buzz seeded for profile '${name}'"
  done
else
  # When disabled, do not strip private keys from agent profiles — only note.
  log "HERMES_BUZZ_ENABLED=0 — Buzz left disabled on browser/api-server; agent profiles unchanged"
fi
