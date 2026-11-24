#!/usr/bin/env bash
# make doctor's project visibility check on a synthetic stack (no Docker): a
# source under a synced coding root's host folder is visible, though
# CODING_EXTRA_ROOTS holds the root's container path. Plain coding roots and
# uncovered sources report as before.
#
#   bash scripts/test-doctor-synced-roots.sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"

tmp="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$tmp"' EXIT
stack="${tmp}/stack"
data="${tmp}/data"
mkdir -p "${stack}/data/hermes" "${tmp}/bin"
cp -R "${ROOT}/scripts" "${stack}/scripts"
# A docker that is installed but never answers: doctor skips its live probes.
printf '#!/bin/sh\nexit 1\n' > "${tmp}/bin/docker"
chmod +x "${tmp}/bin/docker"

synced="${data}/coding-roots/app"
plain="${tmp}/plain"
elsewhere="${tmp}/elsewhere"
mkdir -p "${synced}/src" "${plain}/lib" "${elsewhere}" "${data}/projects/p"
printf '{"roots": [{"id": "app", "containerPath": "/work/app", "hostPath": "%s", "created": "2026-11-24T00:00:00Z"}]}\n' \
  "$synced" > "${data}/coding-roots/roots.json"
printf 'version: 1\nsources:\n  - id: synced\n    path: %s\n  - id: plain\n    path: %s\n  - id: elsewhere\n    path: %s\n' \
  "${synced}/src" "${plain}/lib" "$elsewhere" > "${data}/projects/p/sources.yaml"
printf 'COMPOSE_PROFILES=core\nASSISTANT_DATA_ROOT=%s\nCODING_EXTRA_ROOTS=/work/app:%s\n' \
  "$data" "$plain" > "${stack}/.env"

out="${tmp}/out"
(cd "$stack" && env PATH="${tmp}/bin:${PATH}" STACK_PYTHON=host bash scripts/doctor.sh) > "$out" 2>&1 || true
has() { grep -qF -- "$2" "$out" || fail "$1 (output: $(cat "$out"))"; }
hasnt() { if grep -qF -- "$2" "$out"; then fail "$1 (output: $(cat "$out"))"; fi; }

has "source under the synced root's host folder is visible" "  OK  context visible (p): ${synced}/src"
hasnt "no context-root-add for the synced root" "DIR=${synced}/src"
has "source under a plain coding root is visible" "  OK  context visible (p): ${plain}/lib"
has "uncovered source warns as before" \
  " WARN source p lists ${elsewhere} but it is not a context/coding root — run: make context-root-add DIR=${elsewhere}"
has "index summary still runs" "--- Project file context ---"

echo "ok doctor-synced-roots.sh"
