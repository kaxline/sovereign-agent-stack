#!/usr/bin/env bash
# Run buzz-admin against the Buzz checkout at BUZZ_LOCAL_DIR_PATH.
# Same path resolution as scripts/buzz-relay.sh.
#
#   make buzz-admin generate-key
#   make buzz-admin -- add-member --pubkey npub1…
#   ./scripts/buzz-admin.sh list-members
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() { printf '[buzz-admin] %s\n' "$*"; }
die() { printf '[buzz-admin] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: ./scripts/buzz-admin.sh [buzz-admin args…]
       make buzz-admin [args…]
       make buzz-admin -- [args with --flags…]

Runs Block Buzz's operator CLI (`buzz-admin`) from BUZZ_LOCAL_DIR_PATH.

Examples:
  make buzz-admin generate-key
  make buzz-admin list-members
  make buzz-admin -- add-member --pubkey npub1…
  make buzz-admin ARGS='add-member --pubkey npub1…'   # optional style
  ./scripts/buzz-admin.sh add-member --pubkey <hex> --role admin

Requires BUZZ_LOCAL_DIR_PATH in root .env (Buzz repo root with Cargo.toml).
Member commands need the Buzz checkout's DB/Redis env (usually its .env):
  DATABASE_URL, REDIS_URL, BUZZ_RELAY_PRIVATE_KEY
See docs/buzz.md and the Buzz repo's NOSTR.md.
Note: GNU Make treats leading --flags as its own options, so use
  make buzz-admin -- add-member --pubkey …
when passing dashed buzz-admin flags.
EOF
}

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

resolve_buzz_dir() {
  local raw
  raw="$(env_get BUZZ_LOCAL_DIR_PATH)"
  [[ -n "$raw" ]] || die "BUZZ_LOCAL_DIR_PATH is unset — add it to .env (path to your Buzz checkout)"
  local dir
  dir="$(expand_user_path "$raw")"
  [[ -d "$dir" ]] || die "BUZZ_LOCAL_DIR_PATH does not exist: ${dir}"
  [[ -f "${dir}/Cargo.toml" ]] \
    || die "No Cargo.toml in ${dir} — is BUZZ_LOCAL_DIR_PATH the Buzz repo root?"
  [[ -d "${dir}/crates/buzz-admin" ]] \
    || die "crates/buzz-admin missing in ${dir} — wrong checkout or outdated Buzz tree?"
  echo "$dir"
}

# Load Buzz checkout .env so DATABASE_URL / REDIS_URL / BUZZ_RELAY_PRIVATE_KEY apply.
load_buzz_env() {
  local dir="$1"
  local envfile="${dir}/.env"
  if [[ -f "$envfile" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$envfile"
    set +a
    log "loaded env from ${envfile}"
  else
    log "no ${envfile} — relying on exported env (member cmds need DATABASE_URL / REDIS_URL / BUZZ_RELAY_PRIVATE_KEY)"
  fi
}

# Derive authority roughly like buzz-admin (host + non-default port).
relay_url_authority() {
  local url="${1:-}"
  local rest hostport host port
  rest="${url#*://}"
  rest="${rest%%/*}"
  hostport="$rest"
  if [[ "$hostport" == \[*\]* ]]; then
    echo "$hostport"
    return 0
  fi
  host="${hostport%%:*}"
  if [[ "$hostport" == *:* ]]; then
    port="${hostport##*:}"
    if [[ "$port" =~ ^[0-9]+$ ]] && [[ "$port" != "80" && "$port" != "443" ]]; then
      echo "${host}:${port}"
      return 0
    fi
  fi
  echo "$host"
}

# Operator hints: which community host is targeted, and Local Dev Invites caveats.
log_tenant_context() {
  local relay="${RELAY_URL:-}"
  local auth
  auth="$(relay_url_authority "$relay")"
  log "RELAY_URL=${relay:-<unset>} → community host authority '${auth}'"

  if [[ "$auth" == "localhost:3000" || "$auth" == "127.0.0.1:3000" ]]; then
    local other="127.0.0.1:3000"
    [[ "$auth" == "127.0.0.1:3000" ]] && other="localhost:3000"
    log "NOTE: '${other}' is a separate community in Postgres — keep Desktop on the same host string as RELAY_URL."
  fi

  if [[ "$*" == *list-members* || "$*" == *add-member* ]]; then
    log "NOTE: these commands manage relay_members (ACL). Desktop Invites only appears when the relay advertises NIP-43 (closed membership) and you are owner/admin."
    log "NOTE: on open Local Dev, add agents to channels with: buzz channels add-member --channel <uuid> --pubkey <agent-hex> (as your human identity)."
  fi
}

run_buzz_admin() {
  local dir="$1"
  shift
  local bin=""
  if [[ -x "${dir}/target/release/buzz-admin" ]]; then
    bin="${dir}/target/release/buzz-admin"
  elif [[ -x "${dir}/target/debug/buzz-admin" ]]; then
    bin="${dir}/target/debug/buzz-admin"
  fi

  cd "$dir"
  if [[ -n "$bin" ]]; then
    log "exec ${bin}"
    exec "$bin" "$@"
  fi
  command -v cargo >/dev/null 2>&1 || die "cargo not found — build buzz-admin or install Rust"
  log "exec cargo run -p buzz-admin -- (first run may compile)"
  exec cargo run -p buzz-admin -- "$@"
}

if [[ "${1:-}" == "help" ]]; then
  usage
  exit 0
fi

DIR="$(resolve_buzz_dir)"
load_buzz_env "$DIR"

if [[ "$#" -eq 0 ]]; then
  usage
  exit 1
fi

log_tenant_context "$@"
run_buzz_admin "$DIR" "$@"
