# Shared .env helpers for stack scripts.
# Source from the repo root:  source "$(dirname "$0")/lib/env.sh"

env_get() {
  local key="$1"
  local default="${2:-}"
  local val=""
  local env_file="${ENV_FILE:-.env}"
  if [[ -f "$env_file" ]]; then
    val="$(grep -E "^${key}=" "$env_file" 2>/dev/null | head -1 | cut -d= -f2- || true)"
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

has_compose_profile() {
  local profile="$1"
  local profiles_csv
  profiles_csv="$(env_get COMPOSE_PROFILES core)"
  [[ ",${profiles_csv}," == *",${profile},"* ]]
}

# Gateway topology: HERMES_GATEWAY_TOPOLOGY (standalone|multiplex) from the
# environment or .env; unset, HERMES_PIN_STANDALONE_GATEWAYS=0 means multiplex.
# Same rule as compose/hermes/019-pin-profile-gateways.sh.
gateway_topology() {
  local t pin
  t="${HERMES_GATEWAY_TOPOLOGY:-$(env_get HERMES_GATEWAY_TOPOLOGY)}"
  t="$(printf '%s' "$t" | tr '[:upper:]' '[:lower:]')"
  case "$t" in
    standalone | multiplex) echo "$t"; return ;;
  esac
  pin="${HERMES_PIN_STANDALONE_GATEWAYS:-$(env_get HERMES_PIN_STANDALONE_GATEWAYS 1)}"
  case "$(printf '%s' "$pin" | tr '[:upper:]' '[:lower:]')" in
    0 | false | no | off) echo multiplex ;;
    *) echo standalone ;;
  esac
}
