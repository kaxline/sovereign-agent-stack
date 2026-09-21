#!/usr/bin/env bash
# Clone (if needed) and set up a local Buzz checkout for this stack, then wire
# assistant .env, start the relay, and install the Hermes buzz CLI.
#
#   make bootstrap-buzz
#   ./scripts/bootstrap-buzz.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BUZZ_REPO_URL="${BUZZ_REPO_URL:-https://github.com/block/buzz.git}"
DEFAULT_BUZZ_DIR="${HOME}/src/buzz"
DEFAULT_RELAY_URL="ws://localhost:3000"

log() { printf '[bootstrap-buzz] %s\n' "$*"; }
die() { printf '[bootstrap-buzz] ERROR: %s\n' "$*" >&2; exit 1; }
warn() { printf '[bootstrap-buzz] warning: %s\n' "$*" >&2; }

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

# Set key only when missing or empty in file.
upsert_env_if_empty() {
  local file="$1"
  local key="$2"
  local value="$3"
  touch "$file"
  if grep -q "^${key}=.\+" "$file" 2>/dev/null; then
    return 0
  fi
  upsert_env "$file" "$key" "$value"
}

expand_user_path() {
  local raw="$1"
  [[ -n "$raw" ]] || return 1
  if [[ "$raw" == "~" ]]; then
    raw="$HOME"
  elif [[ "$raw" == "~/"* ]]; then
    raw="$HOME/${raw#~/}"
  fi
  if [[ "$raw" != /* ]]; then
    raw="${ROOT}/${raw#./}"
  fi
  raw="${raw%/}"
  if [[ -d "$raw" ]]; then
    (cd "$raw" && pwd -P)
  else
    echo "$raw"
  fi
}

[[ -f .env ]] || die ".env missing — run ./scripts/setup.sh first"

command -v git >/dev/null 2>&1 || die "git not found"
command -v just >/dev/null 2>&1 || die "'just' not found — install https://github.com/casey/just"
command -v docker >/dev/null 2>&1 || die "Docker not found — install Docker Desktop"
docker info >/dev/null 2>&1 || die "Docker daemon is not running — start Docker Desktop"

raw_path="$(env_get BUZZ_LOCAL_DIR_PATH)"
if [[ -z "$raw_path" ]]; then
  raw_path="$DEFAULT_BUZZ_DIR"
  log "BUZZ_LOCAL_DIR_PATH unset — defaulting to ${raw_path}"
fi
BUZZ_DIR="$(expand_user_path "$raw_path")"

if [[ -f "${BUZZ_DIR}/Cargo.toml" && ( -f "${BUZZ_DIR}/Justfile" || -f "${BUZZ_DIR}/justfile" ) ]]; then
  log "Reusing existing Buzz checkout at ${BUZZ_DIR}"
else
  if [[ -e "$BUZZ_DIR" ]] && [[ ! -d "$BUZZ_DIR" || -n "$(ls -A "$BUZZ_DIR" 2>/dev/null || true)" ]]; then
    die "Refusing to clone into non-empty path without a Buzz tree: ${BUZZ_DIR}
Set BUZZ_LOCAL_DIR_PATH to an existing Buzz repo or an empty directory."
  fi
  mkdir -p "$(dirname "$BUZZ_DIR")"
  log "Cloning ${BUZZ_REPO_URL} → ${BUZZ_DIR}"
  git clone "$BUZZ_REPO_URL" "$BUZZ_DIR"
fi

[[ -f "${BUZZ_DIR}/Cargo.toml" ]] || die "No Cargo.toml in ${BUZZ_DIR}"
[[ -f "${BUZZ_DIR}/Justfile" || -f "${BUZZ_DIR}/justfile" ]] || die "No Justfile in ${BUZZ_DIR}"

upsert_env .env BUZZ_LOCAL_DIR_PATH "$BUZZ_DIR"
upsert_env_if_empty .env BUZZ_RELAY_URL "$DEFAULT_RELAY_URL"
log "Wired assistant .env (BUZZ_LOCAL_DIR_PATH=${BUZZ_DIR})"

# Hermit-pinned tools (lefthook, cargo wrappers) live under bin/. just bootstrap
# exports PATH for its recipe body, but that does not carry into child
# scripts/dev-setup.sh — so we always prepend here before just setup.
export PATH="${BUZZ_DIR}/bin:${PATH}"
log "Prepended ${BUZZ_DIR}/bin to PATH (Hermit lefthook / toolchain)"

log "Running just setup in ${BUZZ_DIR} (deps, migrations, desktop npm, hooks)…"
(
  cd "$BUZZ_DIR"
  just setup
)

command -v cargo >/dev/null 2>&1 || die "cargo not found — install Rust to build buzz-admin"
if [[ -x "${BUZZ_DIR}/target/release/buzz-admin" || -x "${BUZZ_DIR}/target/debug/buzz-admin" ]]; then
  log "buzz-admin binary already present"
else
  log "Building buzz-admin (one-time compile)…"
  (
    cd "$BUZZ_DIR"
    cargo build -p buzz-admin
  )
fi

if ./scripts/buzz-relay.sh status 2>/dev/null | grep -q 'running'; then
  log "Local relay already running"
else
  log "Starting local relay…"
  ./scripts/buzz-relay.sh start
fi

log "Installing Hermes buzz CLI…"
./scripts/install-buzz-cli.sh

relay_url="$(env_get BUZZ_RELAY_URL "$DEFAULT_RELAY_URL")"

cat <<EOF

Buzz bootstrap complete.

  Checkout:     ${BUZZ_DIR}
  Relay URL:    ${relay_url}
  Relay status: make buzz-relay-status
  Relay logs:   make buzz-relay-logs

Next steps:
  1. Open Buzz Desktop → Local Dev (${relay_url})
  2. Create an employee:
       make hermes-buzz-employee PROFILE=software-engineer DISPLAY_NAME="Software Engineer"
  3. Recreate Hermes so s6 picks up the gateway:
       docker compose up -d --force-recreate hermes

See docs/buzz.md.
EOF
