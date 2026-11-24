#!/usr/bin/env bash
# The shared-Postgres slug rule in its three copies agrees on one list:
# db_slug_ok in scripts/lib/env.sh (db.sh, doctor.sh), the service loop in
# compose/postgres/provision.sh, and data.schemas in addon_manifest.py.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
# shellcheck source=lib/env.sh
. "${ROOT}/scripts/lib/env.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

long58="a$(printf '%057d' 0 | tr 0 b)"
long59="${long58}b"
good=(a todo_x a1 x_ pg pgx pg1 agents public_x information_schema_x "$long58")
bad=(agent public information_schema pg_ pg_x pg_catalog Bad Zed 1a _a a-b 'a;b' 'a b' "$long59")

# --- db_slug_ok --------------------------------------------------------------
for s in "${good[@]}"; do db_slug_ok "$s" || fail "db_slug_ok refuses ${s}"; done
for s in "${bad[@]}" ""; do ! db_slug_ok "$s" || fail "db_slug_ok accepts '${s}'"; done
echo "ok: db_slug_ok"

# --- provision.sh ------------------------------------------------------------
# A psql stub that swallows the SQL; a slug is accepted when provision.sh logs
# that it reconciles it.
mkdir -p "${tmp}/bin" "${tmp}/services"
printf '#!/bin/sh\ncat > /dev/null\n' > "${tmp}/bin/psql"
chmod +x "${tmp}/bin/psql"
for s in "${good[@]}" "${bad[@]}"; do
  printf 'DB_SERVICE_PASSWORD=x\n' > "${tmp}/services/${s}.env"
done
PATH="${tmp}/bin:${PATH}" DB_SERVICES_DIR="${tmp}/services" PGDATABASE=t PGPASSWORD=x \
  DB_AGENT_RO_PASSWORD=x DB_AGENT_RW_PASSWORD=x \
  sh "${ROOT}/compose/postgres/provision.sh" > "${tmp}/prov" 2>&1 \
  || { cat "${tmp}/prov" >&2; fail "provision.sh exits non-zero"; }
for s in "${good[@]}"; do
  grep -qF "Reconciling service schema '${s}'" "${tmp}/prov" || fail "provision.sh skips ${s}"
done
for s in "${bad[@]}"; do
  ! grep -qF "Reconciling service schema '${s}'" "${tmp}/prov" || fail "provision.sh accepts ${s}"
done
echo "ok: provision.sh"

# --- addon_manifest data.schemas ---------------------------------------------
# Prints the slugs _validate_data accepts as a one-item data.schemas.
accepted="$(python3 - "${ROOT}/scripts/lib" "${good[@]}" "${bad[@]}" << 'EOF'
import sys
sys.path.insert(0, sys.argv[1])
import addon_manifest as m
for s in sys.argv[2:]:
    if not m._validate_data({"schemas": [s]}, None):
        print(s)
EOF
)"
expected="$(printf '%s\n' "${good[@]}")"
[ "$accepted" = "$expected" ] || fail "addon_manifest data.schemas accepts: $(echo "$accepted" | tr '\n' ' ')"
echo "ok: addon_manifest data.schemas"
