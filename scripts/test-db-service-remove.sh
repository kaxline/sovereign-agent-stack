#!/usr/bin/env bash
# scripts/db.sh service-remove against a stub docker: it needs --yes and a
# valid slug, passes the schema and role to psql, removes the service file
# only after the drop succeeds, and keeps it when psql fails.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
stack="${tmp}/stack"
mkdir -p "${stack}/scripts/lib" "${stack}/compose/postgres/services" "${tmp}/bin"
cp "${ROOT}/scripts/db.sh" "${stack}/scripts/"
cp "${ROOT}/scripts/lib/data-root.sh" "${stack}/scripts/lib/"
printf 'DB_NAME=testdb\n' > "${stack}/.env"

# docker stub: `info` succeeds; `compose exec` logs its argv and stdin, and
# exits 1 when PSQL_FAIL=1.
cat > "${tmp}/bin/docker" <<'EOF'
#!/bin/sh
[ "$1" = info ] && exit 0
printf '%s\n' "$*" >> "${STUB_LOG}"
cat >> "${STUB_LOG}"
[ "${PSQL_FAIL:-0}" = 1 ] && { echo "ERROR: boom" >&2; exit 1; }
exit 0
EOF
chmod +x "${tmp}/bin/docker"
export PATH="${tmp}/bin:${PATH}" STUB_LOG="${tmp}/log"

svc="${stack}/compose/postgres/services/todo_x.env"
run() { rc=0; "${stack}/scripts/db.sh" "$@" > "${tmp}/out" 2>&1 || rc=$?; }

: > "$STUB_LOG"
printf 'DB_SERVICE_PASSWORD=x\n' > "$svc"
run service-remove todo_x
[ "$rc" != 0 ] && grep -q -- '--yes' "${tmp}/out" && [ ! -s "$STUB_LOG" ] && [ -f "$svc" ] \
  || fail "service-remove without --yes refuses and touches nothing"
echo "ok: service-remove needs --yes"

for slug in agent public pg_x Bad 'a;b'; do
  run service-remove "$slug" --yes
  [ "$rc" != 0 ] && [ ! -s "$STUB_LOG" ] || fail "service-remove refuses slug ${slug}"
done
echo "ok: service-remove refuses reserved and invalid slugs"

PSQL_FAIL=1 run service-remove todo_x --yes
[ "$rc" != 0 ] && [ -f "$svc" ] || fail "a failed drop keeps services/todo_x.env"
echo "ok: a failed drop keeps the service file"

: > "$STUB_LOG"
run service-remove todo_x --yes
[ "$rc" = 0 ] || { cat "${tmp}/out"; fail "service-remove todo_x --yes exits 0"; }
grep -q -- 'compose exec -T postgres-db psql .* -d testdb -v schema=todo_x -v role=svc_todo_x' "$STUB_LOG" \
  || { cat "$STUB_LOG"; fail "psql gets the database, schema and role"; }
grep -q 'DROP SCHEMA IF EXISTS :"schema" CASCADE;' "$STUB_LOG" \
  && grep -q 'DROP OWNED BY' "$STUB_LOG" && grep -q 'DROP ROLE IF EXISTS :"role";' "$STUB_LOG" \
  || { cat "$STUB_LOG"; fail "the SQL drops the schema, what the role owns, and the role"; }
[ ! -e "$svc" ] || fail "service-remove deletes services/todo_x.env"
echo "ok: service-remove drops schema and role, then deletes the service file"

run service-remove todo_x --yes
[ "$rc" = 0 ] || fail "service-remove with no service file still runs the drop"
echo "ok: service-remove works when the service file is already gone"
echo "all db.sh service-remove checks passed"
