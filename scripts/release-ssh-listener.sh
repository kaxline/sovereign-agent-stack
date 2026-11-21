#!/usr/bin/env bash
# Write the release copy of scripts/ssh-listener.sh, with the image pinned by
# digest and the release's version, and a .sha256 beside it.
#
#   scripts/release-ssh-listener.sh VERSION IMAGE@sha256:DIGEST OUTDIR
#
# The release workflow runs it after pushing the image to GHCR.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-}"
REF="${2:-}"
OUT="${3:-}"
usage="usage: $0 vX.Y.Z IMAGE@sha256:DIGEST OUTDIR"
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "$usage" >&2; exit 2; }
[[ "$REF" =~ ^[a-z0-9./_-]+@sha256:[0-9a-f]+$ ]] || { echo "$usage" >&2; exit 2; }
[[ -n "$OUT" ]] || { echo "$usage" >&2; exit 2; }

mkdir -p "$OUT"
target="${OUT}/ssh-listener.sh"
sed -e "s|^RELEASE_IMAGE=\"\"\$|RELEASE_IMAGE=\"${REF}\"|" \
  -e "s|^RELEASE_VERSION=\"\"\$|RELEASE_VERSION=\"${VERSION}\"|" \
  "${ROOT}/scripts/ssh-listener.sh" > "$target"
if ! grep -qx "RELEASE_IMAGE=\"${REF}\"" "$target" || ! grep -qx "RELEASE_VERSION=\"${VERSION}\"" "$target"; then
  echo "error: scripts/ssh-listener.sh has no empty RELEASE_IMAGE or RELEASE_VERSION line" >&2
  exit 1
fi
chmod 755 "$target"

if command -v sha256sum > /dev/null 2>&1; then
  sum="$(sha256sum "$target" | cut -d' ' -f1)"
else
  sum="$(shasum -a 256 "$target" | cut -d' ' -f1)"
fi
printf '%s  %s\n' "$sum" ssh-listener.sh > "${target}.sha256"
printf '%s\n' "$target"
