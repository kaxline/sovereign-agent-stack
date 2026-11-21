# The Twenty MCP probe shared by scripts/crm.sh (add, connect) and make
# doctor (scripts/lib/doctor-twenty.sh). Both take a base URL, probe
# <url>/mcp?mode=meta as Hermes registers it, and read the key from a file.
# The key goes to curl in a 0600 header file, never argv, so ps never shows
# it. Call each function in $(...): the EXIT trap that removes its temp files
# belongs to that subshell.
#
# Needs ROOT (the repo root). Python runs through stack_python, so the host
# needs no python3; STACK_PYTHON=host picks the host's, as doctor does when
# it has one.
# shellcheck shell=bash

# shellcheck source=lib/python.sh
declare -F stack_python > /dev/null || source "$(dirname "${BASH_SOURCE[0]}")/python.sh"

# Seconds curl waits for each request. crm.sh's timeout message names it.
MCP_PROBE_TIMEOUT=20

# The tools Hermes reaches records through. Since Twenty 2.56 bare /mcp lists
# every record tool instead; ?mode=meta (what Hermes registers) lists these.
CATALOG_TOOLS="get_tool_catalog learn_tools execute_tool"

# mcp_probe_post URL KEYFILE OUT DATA: POST DATA to URL/mcp?mode=meta with the
# key's header file, the body to OUT. Prints the HTTP status; returns curl's
# exit status.
mcp_probe_post() {
  local hdr rc=0
  hdr="$(umask 077 && mktemp)"
  # shellcheck disable=SC2064  # the path is fixed now
  trap "rm -f '$hdr'" EXIT
  printf 'Authorization: Bearer %s\nContent-Type: application/json\nAccept: application/json, text/event-stream\n' "$(cat "$2")" > "$hdr"
  curl -s -o "$3" -m "$MCP_PROBE_TIMEOUT" -w '%{http_code}' -H @"$hdr" -X POST "${1}/mcp?mode=meta" \
    --data "$4" 2>/dev/null || rc=$?
  rm -f "$hdr"
  return "$rc"
}

# mcp_status URL KEYFILE: HTTP status of an MCP initialize with the key, or
# "timeout" when curl gave up (exit 28).
mcp_status() {
  local code rc=0
  code="$(mcp_probe_post "$1" "$2" /dev/null \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"stack-mcp-probe","version":"0"}}}')" || rc=$?
  if [[ "$rc" -eq 28 ]]; then
    printf 'timeout'
  else
    printf '%s' "${code:-000}"
  fi
}

# catalog_check URL KEYFILE: tools/list with the key, as one line:
#   ok <count>                  every catalog tool is listed
#   missing <count> <tools...>  the server lists tools but not those
#   unknown <reason>            no readable tool list (left to the caller)
catalog_check() {
  local dir code rc=0 name value count="" missing="" err=""
  dir="$(umask 077 && mktemp -d)"
  # shellcheck disable=SC2064  # the path is fixed now
  trap "rm -rf '$dir'" EXIT
  code="$(mcp_probe_post "$1" "$2" "${dir}/body" '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')" || rc=$?
  if [[ "$rc" -ne 0 || "$code" != 200 ]]; then
    printf 'unknown tools/list answered HTTP %s' "${code:-000}"
    return 0
  fi
  # shellcheck disable=SC2086  # one argument per tool name
  while IFS="$(printf '\t')" read -r name value; do
    case "$name" in
      count) count="$value" ;;
      missing) missing="$value" ;;
      error) err="$value" ;;
    esac
  done <<EOF
$(stack_python --mount "$dir" -I "${ROOT}/scripts/lib/mcp_tools_list.py" "${dir}/body" $CATALOG_TOOLS 2>/dev/null)
EOF
  if [[ -z "$count" ]]; then
    printf 'unknown %s' "${err:-no tool list in the answer}"
  elif [[ -n "$missing" ]]; then
    printf 'missing %s %s' "$count" "$missing"
  else
    printf 'ok %s' "$count"
  fi
}
