#!/usr/bin/env bash
# Create a local Hermes profile for a Buzz "employee" under data/hermes/profiles/
# (gitignored). Does not commit anything to the repo.
#
# When BUZZ_PRIVATE_KEY is missing or CHANGE_ME, generates a key via buzz-admin,
# writes it to the profile .env, adds the pubkey to the local relay ACL, and
# joins BUZZ_DEFAULT_CHANNEL (default: general).
#
# Usage:
#   make hermes-buzz-employee PROFILE=software-engineer DISPLAY_NAME="Software Engineer"
#   ./scripts/hermes-buzz-employee.sh software-engineer "Software Engineer"
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

# Read KEY= from a specific env file (profile .env), not root.
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

# Resolve channel name or UUID → UUID. Uses Buzz Postgres when available, else
# buzz channels search inside hermes.
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
      # Only interpolate safe channel names (default: general / Welcome).
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

# Run buzz CLI in hermes with the given private key (never echo the key).
# Rewrite localhost/127.0.0.1 → host.docker.internal so the container reaches
# the host-side `just relay` (Desktop still uses ws://localhost:3000).
# NOTE: host.docker.internal is a *different community host* than localhost —
# prefer host_buzz() for membership ops against Local Dev.
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

# Prefer host-native buzz from BUZZ_LOCAL_DIR_PATH so RELAY_URL host matches
# Desktop (localhost:3000). Container rewrite breaks community lookup.
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

# True if CLI stdout/stderr looks like a Buzz JSON error payload.
buzz_output_is_error() {
  local out="$1"
  printf '%s' "$out" | grep -q '"error"'
}

# Channel membership: host buzz + localhost (community match). Fallback: container.
# Prints CLI stdout/stderr to stdout; returns CLI exit code (or 1 if JSON error with rc 0).
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

# Parse buzz-admin generate-key stdout → set PUB_HEX and SEC_HEX.
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

# Decode nsec/npub bech32 → 32-byte hex, or pass through hex64.
# Usage: nostr_to_hex <nsec1|npub1|hex> → prints hex64
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
# Minimal bech32 (BIP-0173) decode for nsec/npub
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

# Nostr/BIP340 x-only pubkey from hex or nsec secret.
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

# Normalize stored pubkey (hex or npub) to hex64.
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

# Prefer BUZZ_OPERATOR_PRIVATE_KEY; fall back to root BUZZ_PRIVATE_KEY (common misconfig).
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
  # buzz-admin often succeeds with exit 0; treat other errors as soft for hosted.
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

  # Private channels (e.g. Welcome) cannot self-join — go straight to operator add.
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
    # If self-join failed because channel is private, fall through to operator.
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

# Publish kind-0 profile so Desktop shows DISPLAY_NAME instead of raw pubkey.
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

PROFILE="${1:-${PROFILE:-}}"
DISPLAY_NAME="${2:-${DISPLAY_NAME:-}}"
ROLE_BLURB="${ROLE_BLURB:-Focus on your specialty; collaborate with humans and other agents when @mentioned.}"

[[ -n "$PROFILE" ]] || die "Usage: $0 <profile-slug> [display-name]
  Example: $0 software-engineer \"Software Engineer\""

case "$PROFILE" in
  *[!a-z0-9_-]*|"")
    die "PROFILE must be a lowercase slug (a-z, 0-9, _, -): got '${PROFILE}'"
    ;;
  browser|api-server|default)
    die "PROFILE '${PROFILE}' is reserved by this stack"
    ;;
esac

if [[ -z "$DISPLAY_NAME" ]]; then
  DISPLAY_NAME="$PROFILE"
fi

[[ -f .env ]] || die ".env missing — run ./scripts/setup.sh first"

if ! docker compose ps --status running hermes 2>/dev/null | grep -q hermes; then
  die "hermes container is not running — make up first"
fi

profile_dir="data/hermes/profiles/${PROFILE}"
if [[ -d "$profile_dir" ]]; then
  log "Profile directory already exists: ${profile_dir}"
else
  log "Creating Hermes profile '${PROFILE}'"
  docker compose exec -T hermes hermes profile create "$PROFILE"
fi

[[ -d "$profile_dir" ]] || die "expected ${profile_dir} after profile create"

template="${ROOT}/compose/hermes/buzz-employee-soul.template.md"
soul="${profile_dir}/SOUL.md"
if [[ -f "$template" ]]; then
  # Only seed SOUL if missing or still a stock placeholder.
  if [[ ! -f "$soul" ]] || grep -q 'Hermes Agent' "$soul" 2>/dev/null; then
    sed -e "s/{{DISPLAY_NAME}}/${DISPLAY_NAME//\//\\/}/g" \
        -e "s/{{ROLE_BLURB}}/${ROLE_BLURB//\//\\/}/g" \
        "$template" > "$soul"
    log "Wrote ${soul}"
  else
    log "Keeping existing ${soul}"
  fi
else
  warn "missing template ${template}"
fi

relay="$(env_get BUZZ_RELAY_URL)"
touch "${profile_dir}/.env"
# Preserve community host (localhost). Container reachability uses the
# buzz-localhost-proxy cont-init service — do not rewrite to host.docker.internal.
if [[ -n "$relay" ]]; then
  upsert_env "${profile_dir}/.env" BUZZ_RELAY_URL "$relay"
fi
upsert_env "${profile_dir}/.env" BUZZ_CLI_PATH "/opt/data/.local/bin/buzz"

AGENT_PUB=""
AGENT_SEC=""
AUTO_OK=0

ensure_agent_key "${profile_dir}/.env"

# Enable Buzz master toggle and append this profile to HERMES_BUZZ_PROFILES.
upsert_env .env HERMES_BUZZ_ENABLED 1
current="$(env_get HERMES_BUZZ_PROFILES)"
current="${current// /}"
if [[ -z "$current" ]]; then
  upsert_env .env HERMES_BUZZ_PROFILES "$PROFILE"
elif echo ",${current}," | grep -q ",${PROFILE},"; then
  log "HERMES_BUZZ_PROFILES already lists ${PROFILE}"
else
  upsert_env .env HERMES_BUZZ_PROFILES "${current},${PROFILE}"
fi

./scripts/sync-buzz-profile.sh

# Ensure s6 will auto-start this profile on next hermes boot (and after recreate).
printf '{"gateway_state":"running"}\n' > "${profile_dir}/gateway_state.json"

# Membership automation (local checkout + relay). Soft-fail for hosted-only setups.
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

# Publish kind-0 profile so Desktop shows DISPLAY_NAME instead of raw pubkey.
if [[ -n "${AGENT_SEC:-}" && -n "${relay:-}" && -n "${DISPLAY_NAME:-}" ]]; then
  publish_buzz_display_name "$AGENT_SEC" "$relay" "$DISPLAY_NAME" "Hermes employee (${PROFILE})" || true
fi

if [[ "$AUTO_OK" -eq 1 && -n "$AGENT_PUB" ]]; then
  next_steps="$(cat <<EOF

Employee profile ready (local only — under data/hermes/, not git):

  Profile:      ${PROFILE}
  Display name: ${DISPLAY_NAME}
  Home:         ${profile_dir}
  Env:          ${profile_dir}/.env
  Pubkey:       ${AGENT_PUB}

Next steps:
  1. Recreate Hermes so s6 registers the new gateway:
       docker compose up -d --force-recreate hermes
  2. @mention ${DISPLAY_NAME} in Buzz (channel: $(env_get BUZZ_DEFAULT_CHANNEL general))

See docs/buzz.md.
EOF
)"
elif [[ "$AUTO_OK" -eq 1 ]]; then
  next_steps="$(cat <<EOF

Employee profile ready (local only — under data/hermes/, not git):

  Profile:      ${PROFILE}
  Display name: ${DISPLAY_NAME}
  Home:         ${profile_dir}
  Env:          ${profile_dir}/.env

Next steps:
  1. Recreate Hermes so s6 registers the new gateway:
       docker compose up -d --force-recreate hermes
  2. For hosted communities: invite the profile pubkey in Desktop → Invites
  3. @mention ${DISPLAY_NAME} in Buzz

See docs/buzz.md.
EOF
)"
else
  next_steps="$(cat <<EOF

Employee profile ready (local only — under data/hermes/, not git):

  Profile:      ${PROFILE}
  Display name: ${DISPLAY_NAME}
  Home:         ${profile_dir}
  Env:          ${profile_dir}/.env

Next steps:
  1. make bootstrap-buzz   # if local Buzz is not set up yet
  2. Ensure BUZZ_PRIVATE_KEY is set in ${profile_dir}/.env
  3. Recreate Hermes:
       docker compose up -d --force-recreate hermes
  4. @mention ${DISPLAY_NAME} in Buzz

See docs/buzz.md.
EOF
)"
fi

printf '%s\n' "$next_steps"
