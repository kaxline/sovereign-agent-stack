#!/usr/bin/env bash
# make doctor on a synthetic stack (no Docker) resolves ASSISTANT_DATA_ROOT the
# same way in every section: ~ and ~/ expand to $HOME, a relative root is under
# the repo, and an exported ASSISTANT_DATA_ROOT wins over .env.
#
#   bash scripts/test-doctor-data-root.sh
# shellcheck disable=SC2088  # literal ~ paths are what these tests feed in
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
stack="${tmp}/stack"
home="${tmp}/home"
mkdir -p "${stack}/data/hermes" "${tmp}/bin" "${home}"
cp -R "${ROOT}/scripts" "${stack}/scripts"
# A docker that is installed but never answers: doctor skips its live probes.
printf '#!/bin/sh\nexit 1\n' > "${tmp}/bin/docker"
chmod +x "${tmp}/bin/docker"

out="${tmp}/out"
doctor() { # [VAR=value ...]: run doctor.sh in the stack; its exit code is ignored
  (cd "$stack" && env HOME="$home" PATH="${tmp}/bin:${PATH}" "$@" bash scripts/doctor.sh) > "$out" 2>&1 || true
}
has() { grep -qF -- "$2" "$out" || fail "$1 (output: $(cat "$out"))"; }
hasnt() { if grep -qF -- "$2" "$out"; then fail "$1 (output: $(cat "$out"))"; fi; }

# A bitwarden instance file makes doctor print its Bitwarden section.
userdata() { # DIR: a data root with the rag workspace and a bitwarden instance
  mkdir -p "$1/inputs/w" "$1/corpora" "$1/addons/bitwarden/instances"
  printf '{\n  "slug": "a"\n}\n' > "$1/addons/bitwarden/instances/a.json"
}

# --- ~/x in .env ---
userdata "${home}/x"
printf 'COMPOSE_PROFILES=core,rag\nASSISTANT_DATA_ROOT=~/x\nWORKSPACE=w\n' > "${stack}/.env"
doctor
has "~/x root is found" "  OK  ASSISTANT_DATA_ROOT=~/x (${home}/x)"
hasnt "~/x root is not checked under the repo" "${stack}/~"
has "corpus section checks the expanded root" "  OK  ${home}/x/inputs/w/ exists"
has "Bitwarden section sees the instance under the expanded root" "--- Bitwarden ---"
printf 'ok: ~/x in .env\n'

# --- bare ~ ---
userdata "${home}"
printf 'COMPOSE_PROFILES=core,rag\nASSISTANT_DATA_ROOT=~\nWORKSPACE=w\n' > "${stack}/.env"
doctor
has "~ root is \$HOME" "  OK  ASSISTANT_DATA_ROOT=~ (${home})"
has "corpus section uses \$HOME" "  OK  ${home}/inputs/w/ exists"
printf 'ok: ~ in .env\n'

# --- relative root ---
userdata "${stack}/data"
printf 'COMPOSE_PROFILES=core,rag\nASSISTANT_DATA_ROOT=./data\nWORKSPACE=w\n' > "${stack}/.env"
doctor
has "./data is under the repo" "  OK  ASSISTANT_DATA_ROOT=./data (${stack}/data)"
has "corpus section uses the repo root" "  OK  ${stack}/data/inputs/w/ exists"
printf 'ok: ./data in .env\n'

# --- missing root warns with the path as written ---
printf 'COMPOSE_PROFILES=core\nASSISTANT_DATA_ROOT=~/nowhere\n' > "${stack}/.env"
doctor
has "missing root warns" " WARN ASSISTANT_DATA_ROOT=~/nowhere missing — run: make data-dir-set DIR=~/nowhere"
printf 'ok: missing root\n'

# --- exported ASSISTANT_DATA_ROOT wins over .env ---
rm -rf "${home}/x/addons" "${home}/inputs" "${home}/addons"
userdata "${tmp}/elsewhere"
printf 'COMPOSE_PROFILES=core,rag\nASSISTANT_DATA_ROOT=~/x\nWORKSPACE=w\n' > "${stack}/.env"
doctor ASSISTANT_DATA_ROOT="${tmp}/elsewhere"
has "environment root is reported" "  OK  ASSISTANT_DATA_ROOT=${tmp}/elsewhere (${tmp}/elsewhere)"
has "corpus section uses the environment root" "  OK  ${tmp}/elsewhere/inputs/w/ exists"
has "Bitwarden section uses the environment root" "--- Bitwarden ---"
printf 'ok: exported ASSISTANT_DATA_ROOT\n'
