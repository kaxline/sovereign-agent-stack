#!/bin/sh
# sync-buzz-profile.sh leaves a stack that never configured Buzz alone: no
# docker run and no write to the browser/api-server configs.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir "${tmp}/bin" "${tmp}/stack"
cp -R "${ROOT}/scripts" "${tmp}/stack/scripts"
mkdir -p "${tmp}/stack/data/hermes/profiles/browser"
: > "${tmp}/stack/data/hermes/profiles/browser/config.yaml"

cat > "${tmp}/bin/docker" <<'EOF'
#!/bin/sh
echo "$*" >> "$DOCKER_LOG"
EOF
chmod +x "${tmp}/bin/docker"

export DOCKER_LOG="${tmp}/docker.log"
export PATH="${tmp}/bin:${PATH}"
cd "${tmp}/stack"

fail=0
expect() {
  want="$1"
  label="$2"
  : > "$DOCKER_LOG"
  ./scripts/sync-buzz-profile.sh > /dev/null 2>&1
  if [ -s "$DOCKER_LOG" ]; then got=runs; else got=skips; fi
  if [ "$got" = "$want" ]; then
    echo "ok: ${label} ${got}"
  else
    echo "FAIL: ${label} ${got}, want ${want}"
    fail=1
  fi
}

printf 'COMPOSE_PROFILES=core\n' > .env
expect skips "no Buzz keys"
printf 'HERMES_BUZZ_ENABLED=0\n' > .env
expect skips "HERMES_BUZZ_ENABLED=0"
printf 'HERMES_BUZZ_ENABLED=0\nBUZZ_RELAY_URL=ws://localhost:3000\n' > .env
expect runs "relay URL set"
printf 'HERMES_BUZZ_ENABLED=0\nHERMES_BUZZ_PROFILES=engineer\n' > .env
expect runs "profile listed"
printf 'HERMES_BUZZ_ENABLED=1\n' > .env
expect runs "HERMES_BUZZ_ENABLED=1"

exit "$fail"
