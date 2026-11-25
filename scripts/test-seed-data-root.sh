#!/usr/bin/env bash
# seed_data_root and seed_file (scripts/lib/data-root.sh): every template in
# scripts/seed/data-root/ lands in a new root with ${DATA_ROOT} filled in,
# existing files are left alone, and setup.sh, data-dir.sh and corpus.sh write
# no seed text of their own.
# shellcheck disable=SC2016  # $1, ${DATA_ROOT} and the like are for the inner bash, grep or awk
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fail=0
bad() { printf 'FAIL: %s\n' "$1" >&2; fail=1; }

# run_lib CODE ARGS...: CODE in a bash that has sourced data-root.sh.
run_lib() {
  local code="$1"
  shift
  ROOT="$REPO" "${BASH:-bash}" -c "set -euo pipefail; source '$REPO/scripts/lib/data-root.sh'; $code" _ "$@"
}

templates="$(cd "$REPO/scripts/seed/data-root" && find . -type f | sed 's|^\./||' | LC_ALL=C sort)"
[[ -n "$templates" ]] || bad "scripts/seed/data-root/ has no templates"

# --- a new root gets every folder and template, ${DATA_ROOT} filled in ---
# A path with characters that sed or a bash replacement would mangle.
root="$tmp/a&b\\c|d"
out="$(run_lib 'seed_data_root "$1"' "$root" 2>&1)" || bad "seed_data_root failed: $out"
for d in projects voice memory inputs rag_storage corpora memory/notes; do
  [[ -d "$root/$d" ]] || bad "$d/ was not created"
done
while IFS= read -r rel; do
  [[ -f "$root/$rel" ]] || { bad "$rel was not seeded"; continue; }
  if grep -n '\${DATA_ROOT}' "$root/$rel" >&2; then bad "$rel still has \${DATA_ROOT}"; fi
done <<< "$templates"
grep -qF "mkdir -p ${root}/voice/my-voice/samples" "$root/voice/README.md" \
  || bad "voice/README.md does not name the data root literally"
n="$(printf '%s\n' "$out" | grep -c '^Wrote ' || true)"
[[ "$n" == "$(printf '%s\n' "$templates" | wc -l | tr -d ' ')" ]] || bad "expected one Wrote line per template, got: $out"

# --- a second run changes nothing and says nothing ---
printf 'mine\n' > "$root/memory/people.md"
printf 'mine\n' > "$root/voice/README.md"
rm "$root/memory/INDEX.md"
out="$(run_lib 'seed_data_root "$1"' "$root" 2>&1)" || bad "second seed_data_root failed: $out"
[[ "$(cat "$root/memory/people.md")" == mine ]] || bad "an existing people.md was replaced"
[[ "$(cat "$root/voice/README.md")" == mine ]] || bad "an existing voice/README.md was replaced"
[[ -f "$root/memory/INDEX.md" ]] || bad "a removed INDEX.md was not seeded again"
[[ "$out" == "Wrote $root/memory/INDEX.md" ]] || bad "second run printed more than the one new file: $out"

# --- seed_file fills each NAME and uses the caller's log ---
out="$(run_lib 'log() { printf "[t] %s\n" "$1"; }; seed_file "$SEED_DIR/getting-started.md" "$1" "WORKSPACE=$2"' \
  "$tmp/ws/getting-started.md" 'kb-1' 2>&1)" || bad "seed_file failed: $out"
grep -qF '(**kb-1**)' "$tmp/ws/getting-started.md" || bad "getting-started.md does not name the workspace"
grep -qF 'make corpus-ingest SLUG=kb-1' "$tmp/ws/getting-started.md" || bad "getting-started.md has no ingest command for the workspace"
if grep -n '\${' "$tmp/ws/getting-started.md" >&2; then bad "getting-started.md has an unfilled placeholder"; fi
[[ "$out" == "[t] Wrote $tmp/ws/getting-started.md" ]] || bad "seed_file did not log through the caller's log: $out"

# --- the scripts carry no seed text of their own ---
for f in setup.sh data-dir.sh corpus.sh; do
  if grep -nE '^(create|seed)_(getting_started|voice_readme|projects_readme|hermes_user_md|memory_[a-z]+)\(\)' \
    "$REPO/scripts/$f" >&2; then
    bad "scripts/$f still defines its own seed function"
  fi
  if grep -nE '^# (Writing voices|Projects|Curated memory|Memory index|Getting started)$' "$REPO/scripts/$f" >&2; then
    bad "scripts/$f still holds seed text"
  fi
done

[[ "$fail" == 0 ]] || exit 1
echo "seed-data-root: ok"
