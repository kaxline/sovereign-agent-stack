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
