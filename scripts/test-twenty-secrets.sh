#!/bin/sh
# compose/twenty/secrets-entrypoint.sh: reads Twenty's secrets from files when
# the *_FILE variables are set, refuses a value and a file together, and never
# prints a secret. Runs the wrapper with a stub in place of Twenty's entrypoint.
# Then the bootstrap's workspace registration (register_addon_instance_servers):
# a workspace key reaches the profile's .env and nothing else.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
wrapper="${ROOT}/compose/twenty/secrets-entrypoint.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# The stub records what Twenty would receive.
cat > "$tmp/entrypoint.sh" <<'EOF'
#!/bin/sh
printf 'ENCRYPTION_KEY=%s\nPG_DATABASE_URL=%s\nARGS=%s\n' "${ENCRYPTION_KEY:-}" "${PG_DATABASE_URL:-}" "$*" > "$OUT"
EOF
chmod +x "$tmp/entrypoint.sh"
printf 'file-key-0123\n' > "$tmp/key"
printf 'p@ss/w:rd\n' > "$tmp/db"
: > "$tmp/empty"

run() {
  env -i PATH="$PATH" OUT="$tmp/out" TWENTY_ENTRYPOINT="$tmp/entrypoint.sh" "$@" \
    sh "$wrapper" node dist/main > "$tmp/stdout" 2> "$tmp/stderr"
}

no_secret_in_output() {
  for s in file-key-0123 'p@ss' value-key value-db; do
    if grep -qF -- "$s" "$tmp/stdout" "$tmp/stderr"; then fail "$1: output contains a secret"; fi
  done
}

# Value mode: passes through untouched.
run ENCRYPTION_KEY=value-key PG_DATABASE_URL=postgres://twenty:value-db@twenty-db:5432/twenty \
  || fail "value mode should start"
grep -qx 'ENCRYPTION_KEY=value-key' "$tmp/out" || fail "value mode keeps ENCRYPTION_KEY"
grep -qx 'PG_DATABASE_URL=postgres://twenty:value-db@twenty-db:5432/twenty' "$tmp/out" || fail "value mode keeps PG_DATABASE_URL"
grep -qx 'ARGS=node dist/main' "$tmp/out" || fail "arguments reach Twenty's entrypoint"
no_secret_in_output "value mode"

# File mode: both read, password URL-encoded.
rm -f "$tmp/out"
run ENCRYPTION_KEY= PG_DATABASE_URL=postgres://twenty:@twenty-db:5432/twenty \
  TWENTY_ENCRYPTION_KEY_FILE="$tmp/key" TWENTY_DB_PASSWORD_FILE="$tmp/db" \
  || fail "file mode should start"
grep -qx 'ENCRYPTION_KEY=file-key-0123' "$tmp/out" || fail "file mode reads ENCRYPTION_KEY"
grep -qx 'PG_DATABASE_URL=postgres://twenty:p%40ss%2Fw%3Ard@twenty-db:5432/twenty' "$tmp/out" \
  || fail "file mode builds an encoded PG_DATABASE_URL ($(sed -n 2p "$tmp/out" | sed 's/:[^@]*@/:***@/'))"
no_secret_in_output "file mode"

# Value and file together: refused, Twenty not started.
rm -f "$tmp/out"
if run ENCRYPTION_KEY=value-key PG_DATABASE_URL=postgres://twenty:@twenty-db:5432/twenty \
  TWENTY_ENCRYPTION_KEY_FILE="$tmp/key"; then fail "key value plus file must fail"; fi
[ ! -e "$tmp/out" ] || fail "Twenty must not start when the key is set twice"
grep -q 'TWENTY_ENCRYPTION_KEY' "$tmp/stderr" || fail "the error names TWENTY_ENCRYPTION_KEY"
no_secret_in_output "key twice"

if run ENCRYPTION_KEY=value-key PG_DATABASE_URL=postgres://twenty:value-db@twenty-db:5432/twenty \
  TWENTY_DB_PASSWORD_FILE="$tmp/db"; then fail "db value plus file must fail"; fi
grep -q 'TWENTY_DB_PASSWORD' "$tmp/stderr" || fail "the error names TWENTY_DB_PASSWORD"
no_secret_in_output "db twice"

# An empty or missing file is an error, not an empty secret.
if run ENCRYPTION_KEY= PG_DATABASE_URL=postgres://twenty:value-db@twenty-db:5432/twenty \
  TWENTY_ENCRYPTION_KEY_FILE="$tmp/empty"; then fail "empty key file must fail"; fi
if run ENCRYPTION_KEY= PG_DATABASE_URL=postgres://twenty:value-db@twenty-db:5432/twenty \
  TWENTY_ENCRYPTION_KEY_FILE="$tmp/missing"; then fail "missing key file must fail"; fi

# Workspace keys in the bootstrap: the instance store's key files are copied
# into the profile's .env; the plan, the log and the config writer see the
# env key name only.
# upsert_env uses $tmp as its own scratch name.
work="$tmp"
trap 'rm -rf "$work"' EXIT
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
load_fns "$bootstrap" addon_instances_plan addon_instance_key_env addon_instances_warn_none \
  register_addon_instance_servers upsert_env replace_file
ADDONS_DIR="${ROOT}/addons"
ADDON_MANIFEST_PY="${ROOT}/scripts/lib/addon_manifest.py"
ADDON_DATA_DIR="$work/addon-data"
mkdir -p "$ADDON_DATA_DIR/twenty/instances" "$ADDON_DATA_DIR/twenty/secrets"
printf '{"url": "https://crm.example.com", "access": "write"}\n' > "$ADDON_DATA_DIR/twenty/instances/main.json"
printf 'rw-ws-key-4821' > "$ADDON_DATA_DIR/twenty/secrets/main.key_rw"
printf 'ro-ws-key-4821' > "$ADDON_DATA_DIR/twenty/secrets/main.key_ro"
log() { printf '%s\n' "$1" >> "$work/bootstrap.log"; }
profile_env_file() { echo "$work/profile.env"; }
write_bearer_mcp_entry() { printf '%s\n' "$*" >> "$work/bootstrap.log"; }
pin_mcp_tools() { printf '%s\n' "$*" >> "$work/bootstrap.log"; }
reconcile_addon_instance_servers() { printf '%s\n' "$*" >> "$work/bootstrap.log"; }
for profile in "" api-server; do
  TWENTY_MCP_ENABLED=1 TWENTY_LOCAL_ENABLED=0 python3 "$ADDON_MANIFEST_PY" instances-plan \
    "$ADDONS_DIR" "$ADDON_DATA_DIR" "${profile:-default}" >> "$work/bootstrap.log" 2>&1
  export TWENTY_MCP_ENABLED=1 TWENTY_LOCAL_ENABLED=0
  register_addon_instance_servers "$profile" 2>> "$work/bootstrap.log"
done
grep -q 'CRM_MAIN_KEY' "$work/bootstrap.log" || fail "the bootstrap registered crm-main"
if grep -q 'ws-key-4821' "$work/bootstrap.log"; then fail "a workspace key reached the bootstrap log or plan"; fi
grep -qx 'CRM_MAIN_KEY=ro-ws-key-4821' "$work/profile.env" || fail "the key reached the profile .env"

printf 'OK Twenty secrets entrypoint and workspace keys\n'
