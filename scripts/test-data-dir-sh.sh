#!/usr/bin/env bash
# scripts/lib/data-root.sh's expand_user_path, and scripts/data-dir.sh show,
# set and migrate (with and without --remove-source) on a synthetic stack with
# a fake docker.
#
#   bash scripts/test-data-dir-sh.sh
# shellcheck disable=SC2088  # literal ~ paths are what these tests feed in
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
# Resolved, so paths compare equal after data-dir.sh's pwd -P (macOS /var).
tmp="$(cd "$tmp" && pwd -P)"
stack="${tmp}/stack"
home="${tmp}/home"
mkdir -p "${stack}/scripts/lib" "${stack}/data/hermes" "${tmp}/bin" "$home"
cp "${ROOT}/scripts/data-dir.sh" "${stack}/scripts/"
cp "${ROOT}/scripts/lib/env.sh" "${ROOT}/scripts/lib/data-root.sh" "${stack}/scripts/lib/"
printf 'state\n' > "${stack}/data/hermes/state.txt"

# --- expand_user_path ---
check_expand() { # IN WANT
  got="$(ROOT=/repo HOME=/home/u bash -c '. "$1" && expand_user_path "$2"' sh "${stack}/scripts/lib/data-root.sh" "$1")"
  [ "$got" = "$2" ] || fail "expand_user_path $1: got $got, want $2"
}
check_expand '~' /home/u
check_expand '~/' /home/u
check_expand '~/x' /home/u/x
check_expand '~/x/' /home/u/x
check_expand ./data /repo/data
check_expand data/sub /repo/data/sub
check_expand /abs/path/ /abs/path
check_expand / /
check_expand '~other/x' '/repo/~other/x'
[ ! -e /repo ] || fail "expand_user_path test needs /repo to not exist"
printf 'ok: expand_user_path\n'

# Fake docker: `compose ps --status running --services` prints $FAKE_RUNNING.
cat > "${tmp}/bin/docker" << 'EOF'
#!/bin/sh
case "$*" in
  "compose ps --status running --services") cat "${FAKE_RUNNING}" ;;
  *) exit 1 ;;
esac
EOF
chmod +x "${tmp}/bin/docker"
: > "${tmp}/running"

out="${tmp}/out"
dd() { # ARGS...: run data-dir.sh in the stack; fails the test on a non-zero exit
  (cd "$stack" && env HOME="$home" PATH="${tmp}/bin:${PATH}" FAKE_RUNNING="${tmp}/running" \
    bash scripts/data-dir.sh "$@") > "$out" 2>&1 || fail "data-dir.sh $* exited non-zero ($(cat "$out"))"
}
dd_fails() { # ARGS...: data-dir.sh must exit non-zero
  if (cd "$stack" && env HOME="$home" PATH="${tmp}/bin:${PATH}" FAKE_RUNNING="${tmp}/running" \
    bash scripts/data-dir.sh "$@") > "$out" 2>&1; then
    fail "data-dir.sh $* should have failed ($(cat "$out"))"
  fi
}
has() { grep -qF -- "$2" "$out" || fail "$1 (output: $(cat "$out"))"; }
env_root() { sed -n 's/^ASSISTANT_DATA_ROOT=//p' "${stack}/.env"; }

# --- show ---
printf 'ASSISTANT_DATA_ROOT=~/ud\n' > "${stack}/.env"
mkdir -p "${home}/ud/projects"
dd show
has "show resolves ~/" "ASSISTANT_DATA_ROOT (resolved): ${home}/ud"
has "show prints the value as configured" "Configured value:              ~/ud"
has "show marks a present subdir" "  ok           projects  ${home}/ud/projects"
has "show marks a missing subdir" "  missing      voice  ${home}/ud/voice"
has "show finds hermes state" "  ok           hermes  ${stack}/data/hermes"
printf 'ok: show\n'

# --- set ---
dd set '~/new'
[ "$(env_root)" = "${home}/new" ] || fail "set wrote ASSISTANT_DATA_ROOT=$(env_root)"
for d in projects voice memory/notes inputs rag_storage corpora; do
  [ -d "${home}/new/${d}" ] || fail "set did not create ${d}"
done
[ -f "${home}/new/memory/INDEX.md" ] || fail "set did not seed memory/INDEX.md"
printf 'ok: set ~/new\n'

dd set ./rel
[ "$(env_root)" = "${stack}/rel" ] || fail "set ./rel wrote $(env_root)"
printf 'ok: set ./rel\n'

dd_fails set ./data/hermes
has "set refuses data/hermes" "must not be ./data/hermes"
printf 'ok: set refuses data/hermes\n'

# --- migrate <to>, keeping the source ---
printf 'ASSISTANT_DATA_ROOT=./data\n' > "${stack}/.env"
mkdir -p "${stack}/data/memory" "${stack}/data/projects/p"
printf 'm\n' > "${stack}/data/memory/m.md"
printf 'p\n' > "${stack}/data/projects/p/f.txt"
dd migrate '~/moved'
[ "$(env_root)" = "${home}/moved" ] || fail "migrate wrote $(env_root)"
[ "$(cat "${home}/moved/memory/m.md")" = m ] || fail "migrate did not copy memory/m.md"
[ "$(cat "${home}/moved/projects/p/f.txt")" = p ] || fail "migrate did not copy projects/p"
[ -f "${stack}/data/memory/m.md" ] || fail "migrate without --remove-source removed the source"
has "migrate says the source is left" "Source copies left in place under ${stack}/data"
printf 'ok: migrate keeps the source\n'

# --- migrate <from> <to> --remove-source ---
dd migrate ./data '~/moved2' --remove-source
[ "$(env_root)" = "${home}/moved2" ] || fail "migrate --remove-source wrote $(env_root)"
[ "$(cat "${home}/moved2/memory/m.md")" = m ] || fail "migrate --remove-source did not copy memory"
for d in projects voice memory inputs rag_storage corpora; do
  [ ! -e "${stack}/data/${d}" ] || fail "migrate --remove-source left data/${d}"
done
[ "$(cat "${stack}/data/hermes/state.txt")" = state ] || fail "migrate --remove-source touched data/hermes"
printf 'ok: migrate --remove-source\n'

# --- migrate refusals ---
printf 'hermes\n' > "${tmp}/running"
dd_fails migrate '~/moved2' '~/moved3'
has "migrate refuses while hermes runs" "Stop the stack before migrating (running: hermes"
: > "${tmp}/running"
dd_fails migrate '~/moved2' '~/moved2/inner'
has "migrate refuses nested roots" "must not nest"
printf 'ok: migrate refusals\n'
