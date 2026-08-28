#!/usr/bin/env bash
# Sync COMPOSE_PROFILES and data/hermes/.env from HERMES_SIGNAL_ENABLED.
# Called by make ensure-local and scripts/setup.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() { printf '==> %s\n' "$*"; }
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

profiles_list() {
  local current
  current="$(env_get COMPOSE_PROFILES core)"
  current="${current//hermes-webui/}"
  current="${current// /}"
  echo "$current" | tr ',' '\n' | sed '/^$/d'
}

append_profile() {
  local profile="$1"
  if profiles_list | grep -qx "$profile"; then
    return 0
  fi
  local joined
  joined="$(profiles_list | paste -sd, -)"
  if [[ -z "$joined" ]]; then
    upsert_env .env COMPOSE_PROFILES "$profile"
  else
    upsert_env .env COMPOSE_PROFILES "${joined},${profile}"
  fi
  log "Appended '${profile}' to COMPOSE_PROFILES"
}

remove_profile() {
  local profile="$1"
  local filtered
  filtered="$(profiles_list | grep -vx "$profile" | paste -sd, - || true)"
  if [[ -z "$filtered" ]]; then
    filtered="core"
  fi
  local before
  before="$(env_get COMPOSE_PROFILES)"
  upsert_env .env COMPOSE_PROFILES "$filtered"
  if [[ "$before" != "$filtered" ]]; then
    log "Removed '${profile}' from COMPOSE_PROFILES"
  fi
}

[[ -f .env ]] || die ".env missing — run ./scripts/setup.sh first"

enabled="$(env_get HERMES_SIGNAL_ENABLED 0)"
# Accept 1/true/yes (case-insensitive).
case "$(echo "$enabled" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes) enabled=1 ;;
  *) enabled=0 ;;
esac

if [[ "$enabled" -eq 1 ]]; then
  account="$(env_get SIGNAL_ACCOUNT)"
  [[ -n "$account" ]] || die "HERMES_SIGNAL_ENABLED=1 requires SIGNAL_ACCOUNT=+E.164 in .env"

  data_dir="$(env_get SIGNAL_CLI_DATA_DIR)"
  if [[ -z "$data_dir" ]]; then
    data_dir="${HOME}/.local/share/signal-cli"
    upsert_env .env SIGNAL_CLI_DATA_DIR "$data_dir"
    log "Set SIGNAL_CLI_DATA_DIR=${data_dir}"
  fi
  # Expand a literal $HOME written in .env.
  data_dir="${data_dir/\$HOME/$HOME}"
  data_dir="${data_dir/#\~/$HOME}"
  [[ -d "$data_dir" ]] || die "SIGNAL_CLI_DATA_DIR does not exist: ${data_dir} (link signal-cli on the host first)"

  if [[ -z "$(env_get SIGNAL_CLI_UID)" ]]; then
    upsert_env .env SIGNAL_CLI_UID "$(id -u)"
    log "Set SIGNAL_CLI_UID=$(id -u) to match host file ownership"
  fi
  if [[ -z "$(env_get SIGNAL_CLI_GID)" ]]; then
    upsert_env .env SIGNAL_CLI_GID "$(id -g)"
    log "Set SIGNAL_CLI_GID=$(id -g) to match host file ownership"
  fi

  append_profile signal

  mkdir -p data/hermes

  allowed="$(env_get SIGNAL_ALLOWED_USERS "$account")"
  home_ch="$(env_get SIGNAL_HOME_CHANNEL "$account")"
  # Guard against a mashed .env line like HOME_CHANNEL=+1…SIGNAL_CLI_DATA_DIR=…
  if [[ "$home_ch" == *"="* || "$allowed" == *"="* ]]; then
    die "SIGNAL_HOME_CHANNEL or SIGNAL_ALLOWED_USERS looks corrupted (contains '='). Fix the line break in .env and re-run."
  fi

  seed_hermes_signal_env() {
    local hermes_env="$1"
    mkdir -p "$(dirname "$hermes_env")"
    touch "$hermes_env"
    upsert_env "$hermes_env" SIGNAL_HTTP_URL "http://signal-cli:8080"
    upsert_env "$hermes_env" SIGNAL_ACCOUNT "$account"
    upsert_env "$hermes_env" SIGNAL_ALLOWED_USERS "$allowed"
    upsert_env "$hermes_env" SIGNAL_HOME_CHANNEL "$home_ch"
  }

  # Default profile: inbound Signal + cron delivery (gateway connects SSE).
  seed_hermes_signal_env "data/hermes/.env"

  # Browser profile (WebUI): seed credentials for send_message, but keep the
  # Signal *adapter* disabled so this gateway does not fight default for the
  # Signal phone lock (a --replace handoff tears down the whole browser gateway
  # including the WebUI API).
  if [[ -d data/hermes/profiles/browser ]]; then
    seed_hermes_signal_env "data/hermes/profiles/browser/.env"
    browser_cfg="data/hermes/profiles/browser/config.yaml"
    if [[ -f "$browser_cfg" ]]; then
      # Prefer in-container PyYAML when hermes is up; otherwise a minimal append.
      if docker compose ps hermes --status running -q 2>/dev/null | grep -q .; then
        docker compose exec -T hermes python3 - <<'PY'
from pathlib import Path
import yaml
path = Path("/opt/data/profiles/browser/config.yaml")
data = yaml.safe_load(path.read_text()) or {}
platforms = data.setdefault("platforms", {})
signal = platforms.setdefault("signal", {})
if signal.get("enabled") is not False:
    signal["enabled"] = False
    platforms["signal"] = signal
    data["platforms"] = platforms
    path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    print("pinned platforms.signal.enabled=false")
else:
    print("platforms.signal.enabled already false")
PY
      elif ! grep -qF 'sync-signal-profile.sh — WebUI send' "$browser_cfg"; then
        printf '\n# sync-signal-profile.sh — WebUI send uses env; default gateway owns SSE.\nplatforms:\n  signal:\n    enabled: false\n' >> "$browser_cfg"
        log "Appended platforms.signal.enabled=false to browser config.yaml"
      fi
    fi
    log "Seeded Signal credentials in browser profile (adapter disabled for WebUI)"
  fi
  log "Seeded Signal adapter vars in data/hermes/.env (SIGNAL_HTTP_URL=http://signal-cli:8080)"
  log "Stop any host signal-cli daemon before make up — only one receiver per account"
else
  remove_profile signal
  clear_hermes_signal_env() {
    local hermes_env="$1"
    [[ -f "$hermes_env" ]] || return 0
    remove_env_key "$hermes_env" SIGNAL_HTTP_URL
    remove_env_key "$hermes_env" SIGNAL_ACCOUNT
    remove_env_key "$hermes_env" SIGNAL_ALLOWED_USERS
    remove_env_key "$hermes_env" SIGNAL_HOME_CHANNEL
  }
  clear_hermes_signal_env "data/hermes/.env"
  clear_hermes_signal_env "data/hermes/profiles/browser/.env"
fi
