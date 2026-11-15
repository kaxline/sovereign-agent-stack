#!/usr/bin/env bash
# Build the release tarball scripts/install.sh installs.
#
#   scripts/release-tarball.sh VERSION OUTDIR [--src DIR]
#
# Writes OUTDIR/sovereign-agent-stack-VERSION.tar.gz and a .sha256 file
# beside it (`<hex>  <name>`, as sha256sum prints). The tarball holds one
# folder, sovereign-agent-stack-VERSION/, with the files of tag VERSION
# (`git archive`), plus:
#
#   .stack-version  VERSION, which scripts/status.sh and install.sh read
#   .stack-files    every file the release ships, one path per line, so an
#                   upgrade can remove the ones a later release drops
#
# --src DIR packs DIR's files instead of a git tag (tests).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-}"
OUT="${2:-}"
SRC=""
[[ "${3:-}" == --src ]] && SRC="${4:-}"
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage: $0 vX.Y.Z OUTDIR [--src DIR]" >&2; exit 2; }
[[ -n "$OUT" ]] || { echo "usage: $0 vX.Y.Z OUTDIR [--src DIR]" >&2; exit 2; }

name="sovereign-agent-stack-${VERSION}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "${work}/${name}" "$OUT"

if [[ -n "$SRC" ]]; then
  (cd "$SRC" && tar -cf - .) | tar -xf - -C "${work}/${name}"
else
  git -C "$ROOT" rev-parse -q --verify "refs/tags/${VERSION}" > /dev/null \
    || { echo "error: no tag ${VERSION}" >&2; exit 1; }
  git -C "$ROOT" archive --format=tar "$VERSION" | tar -xf - -C "${work}/${name}"
fi

printf '%s\n' "$VERSION" > "${work}/${name}/.stack-version"
(cd "${work}/${name}" && find . -type f ! -name .stack-files | sed 's|^\./||' | LC_ALL=C sort) \
  > "${work}/${name}/.stack-files"

tarball="${OUT}/${name}.tar.gz"
# A fixed owner, so the tarball doesn't carry the builder's user.
if tar --version 2> /dev/null | grep -q GNU; then
  tar -czf "$tarball" --owner=0 --group=0 --numeric-owner -C "$work" "$name"
else
  tar -czf "$tarball" --uid 0 --gid 0 -C "$work" "$name"
fi

if command -v sha256sum > /dev/null 2>&1; then
  sum="$(sha256sum "$tarball" | cut -d' ' -f1)"
else
  sum="$(shasum -a 256 "$tarball" | cut -d' ' -f1)"
fi
printf '%s  %s\n' "$sum" "${name}.tar.gz" > "${tarball}.sha256"
printf '%s\n' "$tarball"
