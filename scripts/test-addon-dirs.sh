#!/usr/bin/env bash
# scripts/addon-dirs.sh: creates every add-on's config, instances and secrets
# at 0700 under a scratch data root, including the two the Hermes bootstraps
# bind-mount, and leaves existing files alone. A data root it refuses is a
# warning with exit 0.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mode_of() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }

data="${tmp}/data"
ASSISTANT_DATA_ROOT="$data" "${ROOT}/scripts/addon-dirs.sh" 2> "${tmp}/err" || fail "addon-dirs.sh exits 0"
[ ! -s "${tmp}/err" ] || fail "no warnings on a fresh root: $(cat "${tmp}/err")"

for m in "${ROOT}"/addons/*/addon.json; do
  sid="$(basename "$(dirname "$m")")"
  for sub in "" /config /instances /secrets; do
    d="${data}/addons/${sid}${sub}"
    [ -d "$d" ] || fail "creates ${d}"
    [ "$(mode_of "$d")" = 700 ] || fail "${d} is 0700"
  done
done

# Existing files stay, and a second run changes nothing.
printf '{}' > "${data}/addons/twenty/instances/x.json"
printf 'k' > "${data}/addons/twenty/secrets/x.key_rw"
ASSISTANT_DATA_ROOT="$data" "${ROOT}/scripts/addon-dirs.sh" || fail "a second run exits 0"
[ "$(cat "${data}/addons/twenty/secrets/x.key_rw")" = k ] || fail "keeps an existing secret"
[ -f "${data}/addons/twenty/instances/x.json" ] || fail "keeps an existing instance"

# A data root inside ./data/hermes is refused with a warning, exit 0.
ASSISTANT_DATA_ROOT="${ROOT}/data/hermes/nope" "${ROOT}/scripts/addon-dirs.sh" 2> "${tmp}/err" \
  || fail "a refused data root still exits 0"
grep -q 'no add-on directories created' "${tmp}/err" || fail "a refused data root is warned about"
[ ! -e "${ROOT}/data/hermes/nope" ] || fail "nothing is created under a refused root"

echo "OK addon-dirs.sh"
