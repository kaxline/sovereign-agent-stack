#!/bin/sh
# Which MCP entries hermes-*-bootstrap gives parked_retry_interval.
#
# Runs set_stack_mcp_parked_retry in the pinned Hermes image, which has the
# YAML module it needs. Skips when Docker or the image is missing, unless
# --image or HERMES_IMAGE_REQUIRED=1 is given.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
# shellcheck source=lib/hermes-image.sh
. "${ROOT}/scripts/lib/hermes-image.sh"
image="$HERMES_IMAGE"
hermes_image_gate "${1:-}" "MCP parked retry"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
{
  grep -E '^(SEARXNG|CALDAV|OPENCODE|SQL|VIKUNJA|MAIL|MAIL_DRAFTS|MESSAGES)_MCP(_RO|_RW)?_URL=' "$bootstrap"
  # LightRAG's URL comes from its manifest (addon_mcp_urls), not from a variable;
  # instance URLs come from addon_instance_urls.
  for fn in addon_mcp_urls addon_instances_plan addon_instance_urls set_stack_mcp_parked_retry; do
    fn_source "$bootstrap" "$fn"
  done
  printf 'ADDONS_DIR=/a\nADDON_MANIFEST_PY=/l/addon_manifest.py\nADDON_DATA_DIR=/t/addon-data\n'
  cat <<'EOF'
log() { printf '%s\n' "$1"; }
config_path_for() { echo /t/config.yaml; }
EOF
} > "${tmp}/fn.sh"

cat > "${tmp}/config.yaml" <<'EOF'
mcp_servers:
  lightrag:
    url: http://lightrag-mcp:8000/mcp
    timeout: 120
  caldav-personal:
    url: http://caldav-mcp:8080/mcp
  calendar-work:
    url: http://caldav-mcp:8080/mcp
  todos:
    url: http://vikunja:3456/api/v2/mcp
    parked_retry_interval: 60
  boundary:
    url: http://host.docker.internal:62441/mcp
  local-tool:
    command: npx
EOF
mkdir -p "${tmp}/addon-data"
chmod -R a+rwX "$tmp"

# run <MCP_PARKED_RETRY_INTERVAL> [profile]: prints the log line, then the config.
run() {
  docker run --rm --network none -v "${tmp}:/t" -v "${ROOT}/addons:/a:ro" \
    -v "${ROOT}/scripts/lib:/l:ro" --entrypoint /bin/sh "$image" -c \
    "MCP_PARKED_RETRY_INTERVAL='$1'; . /t/fn.sh; set_stack_mcp_parked_retry '${2:-}'; echo ---; cat /t/config.yaml"
}

# entry <output> <name>: the lines of one mcp_servers entry.
entry() {
  printf '%s\n' "$1" | sed -n "/^  $2:/,/^  [a-z]/p" | sed '$d'
}

cp "${tmp}/config.yaml" "${tmp}/orig.yaml"
# LightRAG is unmanaged on default (bootstrap never registers it there), yet a
# user's own entry on that URL still gets the interval.
for profile in "" browser; do
  cp "${tmp}/orig.yaml" "${tmp}/config.yaml"
  out="$(run 15 "$profile")"
  for name in lightrag caldav-personal calendar-work todos; do
    entry "$out" "$name" | grep -q 'parked_retry_interval: 15$' || fail "$name not set to 15: $(entry "$out" "$name")"
  done
  for name in boundary local-tool; do
    if entry "$out" "$name" | grep -q parked_retry_interval; then fail "$name is not a stack sidecar"; fi
  done
  entry "$out" lightrag | grep -q 'timeout: 120$' || fail "other keys must survive"
done

out="$(run 15 browser)"
case "$out" in
  Set*) fail "a second run rewrote an unchanged config" ;;
esac

cp "${tmp}/config.yaml" "${tmp}/before.yaml"
out="$(run '')"
cmp -s "${tmp}/config.yaml" "${tmp}/before.yaml" || fail "an empty interval must leave the config alone"

printf 'OK MCP parked retry\n'
