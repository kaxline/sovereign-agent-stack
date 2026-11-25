#!/usr/bin/env bash
# scripts/lib/local-files.sh: ensure_local_files (what up.sh and `make
# ensure-local` run) creates the local overlay files the way setup.sh does,
# with generated keys, and never rewrites files that exist.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
export STACK_PYTHON=host

fail=0
bad() { printf 'FAIL: %s\n' "$1" >&2; fail=1; }

# A scratch ROOT holding the tracked templates and .env.example.
new_root() {
  local root="$1"
  mkdir -p "$root/compose/hermes" "$root/searxng" "$root/opencode"
  cp "$REPO/.env.example" "$root/.env"
  cp "$REPO/compose/hermes/api-server.env.example" "$REPO/compose/hermes/browser.env.example" "$root/compose/hermes/"
  cp "$REPO/searxng/settings.yml" "$root/searxng/"
  cp "$REPO/opencode/opencode.json" "$root/opencode/"
}

run_ensure() {
  # shellcheck disable=SC2016  # expanded by the inner bash
  ROOT="$1" "${BASH:-bash}" -c 'set -euo pipefail; source "$1/scripts/lib/local-files.sh"; ensure_local_files' _ "$REPO"
}

key_of() { sed -n "s/^$2=//p" "$1" | tail -1; }

# --- a fresh checkout gets every file, with keys doctor accepts ---
root="$tmp/fresh"
new_root "$root"
out="$(run_ensure "$root" 2>&1)" || bad "ensure_local_files failed: $out"
for f in compose/hermes/api-server.env compose/hermes/browser.env searxng/settings.local.yml opencode/opencode.local.json; do
  [[ -f "$root/$f" ]] || bad "$f was not created"
done
api_key="$(key_of "$root/compose/hermes/api-server.env" API_SERVER_KEY)"
# doctor.sh fails an API_SERVER_KEY shorter than 16 characters.
[[ "$api_key" =~ ^[0-9a-f]{64}$ ]] || bad "api-server.env API_SERVER_KEY is not a generated key: '$api_key'"
browser_key="$(key_of "$root/compose/hermes/browser.env" API_SERVER_KEY)"
[[ "$browser_key" =~ ^[0-9a-f]{64}$ ]] || bad "browser.env API_SERVER_KEY is not a generated key: '$browser_key'"
[[ "$browser_key" != "$api_key" ]] || bad "api-server.env and browser.env got the same key"
[[ "$(key_of "$root/compose/hermes/browser.env" HERMES_WEBUI_GATEWAY_API_KEY)" == "$browser_key" ]] \
  || bad "browser.env HERMES_WEBUI_GATEWAY_API_KEY does not match its API_SERVER_KEY"
secret="$(key_of "$root/.env" SEARXNG_SECRET)"
[[ "$secret" =~ ^[0-9a-f]{48}$ ]] || bad ".env SEARXNG_SECRET was not replaced with a generated one: '$secret'"
grep -qx "  secret_key: \"${secret}\"" "$root/searxng/settings.local.yml" \
  || bad "settings.local.yml does not carry .env's SEARXNG_SECRET"
grep -q '"baseURL": "http://llm-proxy:4000/v1"' "$root/opencode/opencode.local.json" \
  || bad "opencode.local.json providers do not point at llm-proxy"
case "$out" in *"set API_SERVER_KEY"*) bad "printed the old 'set API_SERVER_KEY' hint" ;; esac

# --- an existing SEARXNG_SECRET is used, not replaced ---
root="$tmp/secret"
new_root "$root"
sed -i.bak 's/^SEARXNG_SECRET=.*/SEARXNG_SECRET=kept-secret/' "$root/.env"
run_ensure "$root" > /dev/null 2>&1 || bad "ensure_local_files failed with a set SEARXNG_SECRET"
[[ "$(key_of "$root/.env" SEARXNG_SECRET)" == kept-secret ]] || bad "a set SEARXNG_SECRET was replaced"
grep -qx '  secret_key: "kept-secret"' "$root/searxng/settings.local.yml" \
  || bad "settings.local.yml does not carry the set SEARXNG_SECRET"

# --- existing files are left byte for byte ---
root="$tmp/existing"
new_root "$root"
printf 'API_SERVER_KEY=mine-api\nOTHER=1\n' > "$root/compose/hermes/api-server.env"
printf 'API_SERVER_KEY=mine-browser\nHERMES_WEBUI_GATEWAY_API_KEY=mine-browser\n' > "$root/compose/hermes/browser.env"
printf 'secret_key: "edited"\nengines: [mine]\n' > "$root/searxng/settings.local.yml"
printf '{"provider": {"x": {"options": {"baseURL": "http://mine:1/v1"}}}}\n' > "$root/opencode/opencode.local.json"
cp "$root/.env" "$root/.env.before"
for f in compose/hermes/api-server.env compose/hermes/browser.env searxng/settings.local.yml opencode/opencode.local.json; do
  cp "$root/$f" "$root/$f.before"
done
out="$(run_ensure "$root" 2>&1)" || bad "ensure_local_files failed over existing files: $out"
for f in compose/hermes/api-server.env compose/hermes/browser.env searxng/settings.local.yml opencode/opencode.local.json .env; do
  cmp -s "$root/$f" "$root/$f.before" || bad "$f was changed"
done
[[ -z "$out" ]] || bad "printed output with every file present: $out"

# --- browser.env without the WebUI alias gets it, matching its key ---
root="$tmp/alias"
new_root "$root"
printf 'API_SERVER_KEY=mine-browser\n' > "$root/compose/hermes/browser.env"
run_ensure "$root" > /dev/null 2>&1 || bad "ensure_local_files failed adding the alias"
[[ "$(key_of "$root/compose/hermes/browser.env" API_SERVER_KEY)" == mine-browser ]] || bad "browser.env key was replaced"
[[ "$(key_of "$root/compose/hermes/browser.env" HERMES_WEBUI_GATEWAY_API_KEY)" == mine-browser ]] \
  || bad "browser.env did not get HERMES_WEBUI_GATEWAY_API_KEY"

# --- up.sh and setup.sh seed through the lib alone ---
if grep -nE 'copy_example|set API_SERVER_KEY' "$REPO/scripts/up.sh" >&2; then
  bad "up.sh still copies the examples itself"
fi
grep -q '^ensure_local_files$' "$REPO/scripts/up.sh" || bad "up.sh does not call ensure_local_files"
n="$(grep -c '^ *ensure_opencode_local_config$' "$REPO/scripts/setup.sh" || true)"
[[ "$n" == 1 ]] || bad "setup.sh calls ensure_opencode_local_config $n times, not once"
if grep -nE '^(ensure_hermes_env_files|sync_searxng_secret|ensure_opencode_local_config)\(\)' \
  "$REPO/scripts/setup.sh" "$REPO/scripts/up.sh" >&2; then
  bad "a local-file function is defined outside scripts/lib/local-files.sh"
fi

[[ "$fail" == 0 ]] || exit 1
echo "local-files: ok"
