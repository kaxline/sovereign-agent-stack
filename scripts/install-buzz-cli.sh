#!/usr/bin/env bash
# Install the headless Buzz CLI into data/hermes/.local/bin (bind-mounted at
# /opt/data/.local/bin inside the hermes container, already on PATH).
#
# Prefers extracting usr/bin/buzz from a pinned Buzz Desktop .deb. On arm64
# (typical Apple Silicon Docker), falls back to a one-shot cargo build when no
# matching .deb exists. Refuses tiny GUI launcher wrappers.
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

VERSION="${BUZZ_CLI_VERSION:-$(env_get BUZZ_CLI_VERSION 0.5.20)}"
# Git ref for cargo fallback (Desktop tag that ships buzz-cli).
GIT_REF="${BUZZ_CLI_GIT_REF:-desktop-v${VERSION}}"
DEST_DIR="${ROOT}/data/hermes/.local/bin"
DEST="${DEST_DIR}/buzz"

container_arch() {
  if command -v docker >/dev/null 2>&1 \
    && docker compose ps --status running hermes 2>/dev/null | grep -q hermes; then
    docker compose exec -T hermes uname -m 2>/dev/null || true
  elif command -v docker >/dev/null 2>&1; then
    docker info --format '{{.Architecture}}' 2>/dev/null || true
  else
    uname -m
  fi
}

ARCH_RAW="$(container_arch)"
ARCH_RAW="$(echo "$ARCH_RAW" | tr -d '\r' | head -1)"
case "$ARCH_RAW" in
  x86_64|amd64) DEB_ARCH="amd64"; RUST_TARGET_HINT="x86_64" ;;
  aarch64|arm64) DEB_ARCH="arm64"; RUST_TARGET_HINT="aarch64" ;;
  *)
    warn "could not detect container arch (${ARCH_RAW:-empty}); assuming amd64"
    DEB_ARCH="amd64"
    RUST_TARGET_HINT="x86_64"
    ;;
esac
if [[ -n "${BUZZ_CLI_DEB_ARCH:-}" ]]; then
  DEB_ARCH="$BUZZ_CLI_DEB_ARCH"
fi

install_binary() {
  local src="$1"
  [[ -f "$src" ]] || die "missing binary: ${src}"
  local size
  size="$(wc -c < "$src" | tr -d ' ')"
  if [[ "$size" -lt 100000 ]]; then
    die "buzz looks like a launcher wrapper (${size} bytes); need the headless CLI"
  fi
  mkdir -p "$DEST_DIR"
  install -m 0755 "$src" "$DEST"
  log "Installed ${DEST} (${size} bytes)"
  if command -v file >/dev/null 2>&1; then
    file "$DEST" || true
  fi
}

verify_in_hermes() {
  if command -v docker >/dev/null 2>&1 \
    && docker compose ps --status running hermes 2>/dev/null | grep -q hermes; then
    if docker compose exec -T hermes buzz --help 2>&1 | head -8 | grep -qiE 'buzz|Usage|CLI|command'; then
      log "Verified: docker compose exec hermes buzz --help"
    else
      warn "buzz is on the volume but --help did not look like the CLI"
    fi
  else
    log "Hermes not running — after make up: docker compose exec hermes buzz --help"
  fi
}

try_deb() {
  local deb_arch="$1"
  local tag="desktop-v${VERSION}"
  local deb_name="Buzz_${VERSION}_${deb_arch}.deb"
  local url="https://github.com/block/buzz/releases/download/${tag}/${deb_name}"
  local tmpdir
  tmpdir="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmpdir'" RETURN

  log "Trying ${deb_name}"
  if ! curl -fsSL --retry 3 -o "${tmpdir}/${deb_name}" "$url"; then
    warn "download failed: ${url}"
    return 1
  fi

  (
    cd "$tmpdir"
    ar x "$deb_name"
    local data_tar
    data_tar="$(find . -maxdepth 1 -name 'data.tar.*' -print | head -1)"
    [[ -n "$data_tar" ]] || return 1
    case "$data_tar" in
      *.xz) tar -xJf "$data_tar" ;;
      *.gz) tar -xzf "$data_tar" ;;
      *.zst) tar --use-compress-program=unzstd -xf "$data_tar" 2>/dev/null || tar -xf "$data_tar" ;;
      *) tar -xf "$data_tar" ;;
    esac
  )

  local src=""
  if [[ -f "${tmpdir}/usr/bin/buzz" ]]; then
    src="${tmpdir}/usr/bin/buzz"
  else
    src="$(find "$tmpdir" -type f -path '*/usr/bin/buzz' 2>/dev/null | head -1 || true)"
  fi
  [[ -n "$src" && -f "$src" ]] || return 1
  install_binary "$src"
  return 0
}

build_with_cargo_docker() {
  command -v docker >/dev/null 2>&1 || die "docker required for cargo fallback"
  local outdir="${ROOT}/data/hermes/.cache/buzz-cli-build"
  mkdir -p "$outdir"
  log "Building buzz-cli via Docker (ref ${GIT_REF}, arch hint ${RUST_TARGET_HINT})"
  log "Release compile usually takes several minutes — cargo progress will print below"
  local platform=""
  case "$DEB_ARCH" in
    arm64) platform="--platform=linux/arm64" ;;
    amd64) platform="--platform=linux/amd64" ;;
  esac
  # shellcheck disable=SC2086
  docker run --rm $platform \
    -v "${outdir}:/out" \
    -e "GIT_REF=${GIT_REF}" \
    rust:1.85-bookworm \
    bash -c '
      set -euo pipefail
      apt-get -qq update && apt-get -qq install -y --no-install-recommends git pkg-config ca-certificates >/dev/null
      git clone --depth 1 --branch "$GIT_REF" https://github.com/block/buzz.git /src \
        || git clone --depth 1 https://github.com/block/buzz.git /src
      cd /src
      git fetch --depth 1 origin "refs/tags/${GIT_REF}:refs/tags/${GIT_REF}" 2>/dev/null || true
      git checkout "$GIT_REF" 2>/dev/null || true
      # Keep cargo output visible — a silent --locked build looks hung for minutes.
      cargo build --release -p buzz-cli --locked || cargo build --release -p buzz-cli
      install -m 0755 target/release/buzz /out/buzz
    '
  [[ -f "${outdir}/buzz" ]] || die "cargo docker build did not produce buzz"
  install_binary "${outdir}/buzz"
}

METHOD="${BUZZ_CLI_METHOD:-auto}"

case "$METHOD" in
  deb)
    try_deb "$DEB_ARCH" || die "deb install failed for arch ${DEB_ARCH}"
    ;;
  cargo)
    build_with_cargo_docker
    ;;
  auto)
    if try_deb "$DEB_ARCH"; then
      :
    elif [[ "$DEB_ARCH" == "amd64" ]]; then
      # Only fall back across arch when the container itself is amd64 (never
      # install x86_64 buzz into an aarch64 hermes — Rosetta won't help Linux).
      die "deb install failed for amd64; try BUZZ_CLI_METHOD=cargo"
    else
      warn "no ${DEB_ARCH} Buzz Desktop .deb for v${VERSION}; building via cargo"
      build_with_cargo_docker
    fi
    ;;
  *)
    die "BUZZ_CLI_METHOD must be auto|deb|cargo (got ${METHOD})"
    ;;
esac

verify_in_hermes
log "Done. Binary persists under data/hermes/.local/bin (gitignored)."
