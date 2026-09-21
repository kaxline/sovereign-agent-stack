#!/usr/bin/env bash
# Attach Buzz transport to an existing independent agent profile.
# Keygen, HERMES_BUZZ_PROFILES, sync, relay ACL, channel join, display name.
#
# Usage:
#   ./scripts/agent-attach-buzz.sh software-engineer
#   DISPLAY_NAME="Software Engineer" ./scripts/agent-attach-buzz.sh software-engineer
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
warn() { printf 'warning: %s\n' "$*" >&2; }

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

file_env_get() {
  local file="$1"
  local key="$2"
  local default="${3:-}"
  local val=""
  if [[ -f "$file" ]]; then
    val="$(grep -E "^${key}=" "$file" 2>/dev/null | head -1 | cut -d= -f2- || true)"
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

is_uuid() {
  [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]
}

is_hex64() {
  [[ "$1" =~ ^[0-9a-fA-F]{64}$ ]]
}

# shellcheck source=lib/agent-registry.sh
source "${ROOT}/scripts/lib/agent-registry.sh"

resolve_channel_id() {
  local name_or_id="$1"
  local agent_key="$2"
  local relay_url="$3"

  if is_uuid "$name_or_id"; then
    echo "$name_or_id"
    return 0
  fi

  local buzz_dir raw dburl
  raw="$(env_get BUZZ_LOCAL_DIR_PATH)"
  if [[ -n "$raw" ]]; then
    buzz_dir="$(expand_user_path "$raw")"
    if [[ -f "${buzz_dir}/.env" ]]; then
      dburl="$(grep -E '^DATABASE_URL=' "${buzz_dir}/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
      dburl="${dburl%\"}"
      dburl="${dburl#\"}"
      if [[ -n "$dburl" ]] && command -v psql >/dev/null 2>&1 \
        && [[ "$name_or_id" =~ ^[A-Za-z0-9][A-Za-z0-9_\ -]*$ ]]; then
        local id
        id="$(
          psql "$dburl" -tAc \
            "SELECT id::text FROM channels WHERE lower(name)=lower('${name_or_id}') AND deleted_at IS NULL ORDER BY created_at ASC LIMIT 1;" \
            2>/dev/null | tr -d '[:space:]' || true
        )"
        if is_uuid "$id"; then
          echo "$id"
          return 0
        fi
      fi
    fi
  fi

  if ! docker compose ps --status running hermes 2>/dev/null | grep -q hermes; then
    return 1
  fi

  local search_out
  search_out="$(
    docker compose exec -T \
      -e "BUZZ_PRIVATE_KEY=${agent_key}" \
      -e "BUZZ_RELAY_URL=${relay_url}" \
      hermes buzz channels search --query "$name_or_id" --exact 2>/dev/null || true
  )"
  local id
  id="$(printf '%s' "$search_out" | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    raise SystemExit(1)
data = json.loads(raw)
if isinstance(data, list) and data:
    print(data[0].get("channel_id") or "")
elif isinstance(data, dict):
    print(data.get("channel_id") or "")
' 2>/dev/null || true)"
  if is_uuid "$id"; then
    echo "$id"
    return 0
  fi
  return 1
}

relay_url_for_container() {
  local url="$1"
  url="${url//127.0.0.1/host.docker.internal}"
  url="${url//localhost/host.docker.internal}"
  printf '%s\n' "$url"
}

hermes_buzz() {
  local agent_key="$1"
  local relay_url="$2"
  shift 2
  local container_relay
  container_relay="$(relay_url_for_container "$relay_url")"
  docker compose exec -T \
    -e "BUZZ_PRIVATE_KEY=${agent_key}" \
    -e "BUZZ_RELAY_URL=${container_relay}" \
    hermes buzz "$@"
}

resolve_host_buzz_bin() {
  local raw dir
  raw="$(env_get BUZZ_LOCAL_DIR_PATH)"
  [[ -n "$raw" ]] || return 1
  dir="$(expand_user_path "$raw")"
  if [[ -x "${dir}/target/release/buzz" ]]; then
    echo "${dir}/target/release/buzz"
    return 0
  fi
  if [[ -x "${dir}/target/debug/buzz" ]]; then
    echo "${dir}/target/debug/buzz"
    return 0
  fi
  return 1
}

host_buzz() {
  local agent_key="$1"
  local relay_url="$2"
  shift 2
  local bin
  bin="$(resolve_host_buzz_bin)" || return 1
  BUZZ_PRIVATE_KEY="$agent_key" BUZZ_RELAY_URL="$relay_url" "$bin" "$@"
}

buzz_output_is_error() {
  local out="$1"
  printf '%s' "$out" | grep -q '"error"'
}

run_buzz() {
  local agent_key="$1"
  local relay_url="$2"
  shift 2
  local out rc=0
  if resolve_host_buzz_bin >/dev/null 2>&1; then
    out="$(host_buzz "$agent_key" "$relay_url" "$@" 2>&1)" && rc=0 || rc=$?
  else
    out="$(hermes_buzz "$agent_key" "$relay_url" "$@" 2>&1)" && rc=0 || rc=$?
  fi
  printf '%s\n' "$out"
  if [[ "$rc" -eq 0 ]] && buzz_output_is_error "$out"; then
    return 1
  fi
  return "$rc"
}

parse_generate_key() {
  local out="$1"
  PUB_HEX="$(printf '%s\n' "$out" | awk '/^Public key:/{print $3; exit}')"
  SEC_HEX="$(printf '%s\n' "$out" | awk '/^Secret key:/{print $3; exit}')"
  PUB_HEX="$(printf '%s' "$PUB_HEX" | tr -d '[:space:]')"
  SEC_HEX="$(printf '%s' "$SEC_HEX" | tr -d '[:space:]')"
  is_hex64 "$PUB_HEX" || return 1
  is_hex64 "$SEC_HEX" || return 1
  return 0
}

nostr_to_hex() {
  local raw="$1"
  [[ -n "$raw" ]] || return 1
  if is_hex64 "$raw"; then
    printf '%s\n' "$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')"
    return 0
  fi
  NOSTR_KEY="$raw" python3 - <<'PY' 2>/dev/null || return 1
import os, sys
s = os.environ.get("NOSTR_KEY", "").strip()
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
def bech32_polymod(values):
    GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1ffffff) << 5) ^ v
        for i in range(5):
            chk ^= GEN[i] if ((b >> i) & 1) else 0
    return chk
def bech32_hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]
def bech32_verify(hrp, data):
    return bech32_polymod(bech32_hrp_expand(hrp) + data) == 1
def bech32_decode(bech):
    bech = bech.strip()
    if any(ord(x) < 33 or ord(x) > 126 for x in bech):
        return None, None
    if bech.lower() != bech and bech.upper() != bech:
        return None, None
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech):
        return None, None
    hrp = bech[:pos]
    data = []
    for c in bech[pos + 1 :]:
        d = CHARSET.find(c)
        if d == -1:
            return None, None
        data.append(d)
    if not bech32_verify(hrp, data):
        return None, None
    return hrp, data[:-6]
def convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret
if s.startswith(("nsec1", "npub1")):
    hrp, data = bech32_decode(s)
    if hrp not in ("nsec", "npub") or data is None:
        sys.exit(1)
    decoded = convertbits(data, 5, 8, False)
    if not decoded or len(decoded) != 32:
        sys.exit(1)
    print(bytes(decoded).hex())
else:
    sys.exit(1)
PY
}

derive_pubkey_from_secret() {
  local agent_key="$1"
  local sk_hex
  sk_hex="$(nostr_to_hex "$agent_key")" || return 1
  local pk
  pk="$(
    AGENT_KEY="$sk_hex" python3 - <<'PY' 2>/dev/null || true
import os
sk_hex = os.environ.get("AGENT_KEY", "").strip().lower()
if len(sk_hex) != 64:
    raise SystemExit(1)
try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.backends import default_backend
    sk_int = int(sk_hex, 16)
    priv = ec.derive_private_key(sk_int, ec.SECP256K1(), default_backend())
    print(format(priv.public_key().public_numbers().x, "064x"))
except Exception:
    raise SystemExit(1)
PY
  )"
  if is_hex64 "$pk"; then
    echo "$pk"
    return 0
  fi
  return 1
}

normalize_pubkey_hex() {
  local raw="$1"
  [[ -n "$raw" ]] || return 1
  if is_hex64 "$raw"; then
    printf '%s\n' "$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')"
    return 0
  fi
  if [[ "$raw" == npub1* ]]; then
    nostr_to_hex "$raw"
    return $?
  fi
  return 1
}

resolve_operator_key() {
  local op
  op="$(env_get BUZZ_OPERATOR_PRIVATE_KEY)"
  if [[ -n "$op" ]]; then
    echo "$op"
    return 0
  fi
  op="$(env_get BUZZ_PRIVATE_KEY)"
  if [[ -n "$op" ]]; then
    warn "BUZZ_OPERATOR_PRIVATE_KEY unset — using root BUZZ_PRIVATE_KEY as operator identity"
    echo "$op"
    return 0
  fi
  return 1
}

ensure_agent_key() {
  local profile_env="$1"
  local existing existing_pub
  existing="$(file_env_get "$profile_env" BUZZ_PRIVATE_KEY)"
  existing_pub="$(file_env_get "$profile_env" BUZZ_PUBLIC_KEY)"
  if [[ -n "$existing" && "$existing" != "CHANGE_ME" ]]; then
    AGENT_SEC="$existing"
    log "Keeping existing BUZZ_PRIVATE_KEY in ${profile_env}"
    AGENT_PUB="$(normalize_pubkey_hex "$existing_pub" || true)"
    if ! is_hex64 "$AGENT_PUB"; then
      AGENT_PUB="$(derive_pubkey_from_secret "$AGENT_SEC" || true)"
    fi
    if is_hex64 "$AGENT_PUB"; then
      upsert_env "$profile_env" BUZZ_PUBLIC_KEY "$AGENT_PUB"
    fi
    return 0
  fi

  [[ -n "$(env_get BUZZ_LOCAL_DIR_PATH)" ]] \
    || die "BUZZ_LOCAL_DIR_PATH unset — run make bootstrap-buzz first"

  log "Generating Nostr keypair via buzz-admin…"
  local out
  out="$(./scripts/buzz-admin.sh generate-key 2>&1)" \
    || die "buzz-admin generate-key failed"
  if ! parse_generate_key "$out"; then
    die "Failed to parse buzz-admin generate-key output"
  fi
  AGENT_PUB="$PUB_HEX"
  AGENT_SEC="$SEC_HEX"
  upsert_env "$profile_env" BUZZ_PRIVATE_KEY "$AGENT_SEC"
  upsert_env "$profile_env" BUZZ_PUBLIC_KEY "$AGENT_PUB"
  log "Wrote BUZZ_PRIVATE_KEY to ${profile_env} (pubkey ${AGENT_PUB})"
}

add_relay_member() {
  local pubkey="$1"
  [[ -n "$pubkey" ]] || return 1
  log "Adding pubkey to relay ACL (buzz-admin add-member)…"
  local err
  if err="$(./scripts/buzz-admin.sh add-member --pubkey "$pubkey" 2>&1)"; then
    log "Relay ACL: member added (or already present)"
    return 0
  fi
  if printf '%s' "$err" | grep -qiE 'already|exists|duplicate'; then
    log "Relay ACL: already a member"
    return 0
  fi
  warn "buzz-admin add-member failed (continuing): $(printf '%s' "$err" | tail -3 | tr '\n' ' ')"
  return 0
}

join_default_channel() {
  local agent_key="$1"
  local pubkey="$2"
  local relay_url="$3"
  local channel_name
  channel_name="$(env_get BUZZ_DEFAULT_CHANNEL general)"
  local channel_id=""

  if ! channel_id="$(resolve_channel_id "$channel_name" "$agent_key" "$relay_url")"; then
    warn "Could not resolve channel '${channel_name}' to a UUID — skip channel join"
    warn "Set BUZZ_DEFAULT_CHANNEL to a channel UUID, or open Desktop once so channels exist"
    return 0
  fi
  log "Joining channel ${channel_name} (${channel_id})…"

  local op_key pub_hex
  op_key="$(resolve_operator_key || true)"
  pub_hex="$(normalize_pubkey_hex "$pubkey" || true)"

  local skip_self=0
  if [[ -n "$op_key" ]] && [[ "${channel_name}" == "Welcome" || "${channel_name}" == "welcome" ]]; then
    skip_self=1
  fi

  if [[ "$skip_self" -eq 0 ]]; then
    local join_out join_rc=0
    join_out="$(run_buzz "$agent_key" "$relay_url" channels join --channel "$channel_id" 2>&1)" && join_rc=0 || join_rc=$?
    if [[ "$join_rc" -eq 0 ]]; then
      log "Joined channel ${channel_name}"
      return 0
    fi
    if printf '%s' "$join_out" | grep -qi 'private'; then
      log "Channel is private — need operator add-member"
    else
      log "Self-join failed (rc=${join_rc})"
    fi
  else
    log "Channel '${channel_name}' typically private — using operator add-member"
  fi

  if [[ -n "$op_key" && -n "$pub_hex" ]] && is_hex64 "$pub_hex"; then
    log "Trying operator channels add-member…"
    local add_out add_rc=0
    add_out="$(run_buzz "$op_key" "$relay_url" channels add-member \
      --channel "$channel_id" --pubkey "$pub_hex" --role member 2>&1)" && add_rc=0 || add_rc=$?
    if [[ "$add_rc" -eq 0 ]]; then
      log "Operator added agent to channel ${channel_name}"
      return 0
    fi
    warn "Operator channels add-member failed for ${channel_name}: $(printf '%s' "$add_out" | head -c 160 | tr '\n' ' ')"
    return 0
  fi

  if [[ -n "$pubkey" ]] && ! is_hex64 "$pub_hex"; then
    warn "Operator add-member needs hex pubkey (got non-hex value)"
    return 0
  fi

  warn "Could not join channel ${channel_name}. For private channels set BUZZ_OPERATOR_PRIVATE_KEY in root .env"
  return 0
}

publish_buzz_display_name() {
  local agent_key="$1"
  local relay_url="$2"
  local name="$3"
  local about="${4:-}"
  [[ -n "$agent_key" && -n "$relay_url" && -n "$name" ]] || return 1

  log "Setting Buzz display name to '${name}'…"
  local out rc=0
  local -a args=(users set-profile --name "$name")
  if [[ -n "$about" ]]; then
    args+=(--about "$about")
  fi
  out="$(run_buzz "$agent_key" "$relay_url" "${args[@]}" 2>&1)" && rc=0 || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    warn "buzz users set-profile failed (rc=${rc}): $(printf '%s' "$out" | head -c 160 | tr '\n' ' ')"
    return 1
  fi
  log "Buzz profile display name set to '${name}'"
  return 0
}

NAME="${1:-${NAME:-${PROFILE:-}}}"
DISPLAY_NAME="${DISPLAY_NAME:-}"

[[ -n "$NAME" ]] || die "Usage: $0 <agent-slug>
  Example: $0 software-engineer
  Env: DISPLAY_NAME=…"

case "$NAME" in
  *[!a-z0-9_-]*|"")
    die "NAME must be a lowercase slug (a-z, 0-9, _, -): got '${NAME}'"
    ;;
  browser|api-server|default)
    die "NAME '${NAME}' is reserved by this stack"
    ;;
esac

[[ -f .env ]] || die ".env missing — run ./scripts/setup.sh first"

profile_dir="data/hermes/profiles/${NAME}"
[[ -d "$profile_dir" ]] || die "Agent profile missing at ${profile_dir} — run: make agent-create NAME=${NAME}"

if [[ -z "$DISPLAY_NAME" ]]; then
  if [[ -f "${profile_dir}/SOUL.md" ]]; then
    DISPLAY_NAME="$(awk '/^# /{sub(/^# /,""); print; exit}' "${profile_dir}/SOUL.md" || true)"
  fi
fi
if [[ -z "$DISPLAY_NAME" ]]; then
  DISPLAY_NAME="$NAME"
fi

if ! docker compose ps --status running hermes 2>/dev/null | grep -q hermes; then
  die "hermes container is not running — make up first"
fi

relay="$(env_get BUZZ_RELAY_URL)"
touch "${profile_dir}/.env"
if [[ -n "$relay" ]]; then
  upsert_env "${profile_dir}/.env" BUZZ_RELAY_URL "$relay"
fi
upsert_env "${profile_dir}/.env" BUZZ_CLI_PATH "/opt/data/.local/bin/buzz"

AGENT_PUB=""
AGENT_SEC=""
AUTO_OK=0

ensure_agent_key "${profile_dir}/.env"

upsert_env .env HERMES_BUZZ_ENABLED 1
current="$(env_get HERMES_BUZZ_PROFILES)"
current="${current// /}"
if [[ -z "$current" ]]; then
  upsert_env .env HERMES_BUZZ_PROFILES "$NAME"
elif echo ",${current}," | grep -q ",${NAME},"; then
  log "HERMES_BUZZ_PROFILES already lists ${NAME}"
else
  upsert_env .env HERMES_BUZZ_PROFILES "${current},${NAME}"
fi

./scripts/sync-buzz-profile.sh

printf '{"gateway_state":"running"}\n' > "${profile_dir}/gateway_state.json"

local_buzz="$(env_get BUZZ_LOCAL_DIR_PATH)"
if [[ -z "$relay" ]]; then
  warn "BUZZ_RELAY_URL unset — skip relay ACL / channel join"
elif [[ -z "$local_buzz" ]]; then
  warn "BUZZ_LOCAL_DIR_PATH unset — skip buzz-admin ACL (hosted: invite in Desktop). Key is ready."
  AUTO_OK=1
else
  if [[ -z "$AGENT_PUB" ]]; then
    AGENT_PUB="$(derive_pubkey_from_secret "$AGENT_SEC" || true)"
  fi
  if [[ -z "$AGENT_PUB" ]]; then
    warn "Could not derive pubkey from existing secret — skip ACL/channel"
  else
    add_relay_member "$AGENT_PUB"
    join_default_channel "$AGENT_SEC" "$AGENT_PUB" "$relay"
    AUTO_OK=1
  fi
fi

if [[ -n "${AGENT_SEC:-}" && -n "${relay:-}" && -n "${DISPLAY_NAME:-}" ]]; then
  publish_buzz_display_name "$AGENT_SEC" "$relay" "$DISPLAY_NAME" "Hermes agent (${NAME})" || true
fi

if is_hex64 "${AGENT_PUB:-}"; then
  agent_registry_set_buzz_pubkey "$NAME" "$AGENT_PUB"
  agent_registry_sync_siblings
fi

if [[ "$AUTO_OK" -eq 1 && -n "$AGENT_PUB" ]]; then
  next_steps="$(cat <<EOF

Agent Buzz transport ready (local only — under data/hermes/, not git):

  Agent:        ${NAME}
  Display name: ${DISPLAY_NAME}
  Home:         ${profile_dir}
  Env:          ${profile_dir}/.env
  Pubkey:       ${AGENT_PUB}

Next steps:
  1. Recreate Hermes so s6 registers the gateway:
       docker compose up -d --force-recreate hermes
  2. @mention ${DISPLAY_NAME} in Buzz (channel: $(env_get BUZZ_DEFAULT_CHANNEL general))

See docs/agents.md and docs/buzz.md.
EOF
)"
elif [[ "$AUTO_OK" -eq 1 ]]; then
  next_steps="$(cat <<EOF

Agent Buzz transport ready (local only — under data/hermes/, not git):

  Agent:        ${NAME}
  Display name: ${DISPLAY_NAME}
  Home:         ${profile_dir}
  Env:          ${profile_dir}/.env

Next steps:
  1. Recreate Hermes so s6 registers the gateway:
       docker compose up -d --force-recreate hermes
  2. For hosted communities: invite the profile pubkey in Desktop → Invites
  3. @mention ${DISPLAY_NAME} in Buzz

See docs/agents.md and docs/buzz.md.
EOF
)"
else
  next_steps="$(cat <<EOF

Agent profile has Buzz wiring started (local only — under data/hermes/, not git):

  Agent:        ${NAME}
  Display name: ${DISPLAY_NAME}
  Home:         ${profile_dir}
  Env:          ${profile_dir}/.env

Next steps:
  1. make bootstrap-buzz   # if local Buzz is not set up yet
  2. Ensure BUZZ_PRIVATE_KEY is set in ${profile_dir}/.env
  3. Recreate Hermes:
       docker compose up -d --force-recreate hermes
  4. @mention ${DISPLAY_NAME} in Buzz

See docs/agents.md and docs/buzz.md.
EOF
)"
fi

printf '%s\n' "$next_steps"
