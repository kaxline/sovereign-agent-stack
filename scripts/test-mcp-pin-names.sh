#!/bin/sh
# Pinned MCP tool names must match what Hermes registers: mcp__<server>__<tool>
# with every character outside [A-Za-z0-9_] in the server name turned into _
# (tools/mcp_tool_schema.py sanitize_mcp_name_component). crm-main's tools are
# mcp__crm_main__*, so a pin on mcp__crm-main__* matches nothing.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"

load_fns "$bootstrap" pin_mcp_tools mcp_tool_name

pinned=""
pin_tool_search_always_include() { shift; pinned="$*"; }

[ "$(mcp_tool_name crm-main get_tool_catalog)" = "mcp__crm_main__get_tool_catalog" ] || fail "crm-main -> mcp__crm_main__"
[ "$(mcp_tool_name caldav-work-cal caldav_get_events)" = "mcp__caldav_work_cal__caldav_get_events" ] || fail "caldav-work-cal"
[ "$(mcp_tool_name todos tasks_list)" = "mcp__todos__tasks_list" ] || fail "a plain key stays as is"

# Over 64 characters Hermes cuts the name to 55 and adds _<8 hex of its SHA-256>
# (mcp_prefixed_tool_name). A 31-character Twenty slug does that to
# list_object_metadata_names; 64 exactly is kept.
slug31="crm-abcdefghijklmnopqrstuvwxyz01234"
long="$(mcp_tool_name "$slug31" list_object_metadata_names)"
want="$(python3 -I -c 'import hashlib, sys
n = sys.argv[1]
print(n[:55] + "_" + hashlib.sha256(n.encode()).hexdigest()[:8])' "mcp__crm_abcdefghijklmnopqrstuvwxyz01234__list_object_metadata_names")"
[ "$long" = "$want" ] || fail "a name over 64 characters is clamped as Hermes does ($long, want $want)"
[ "${#long}" -eq 64 ] || fail "the clamped name is 64 characters (${#long})"
exact="$(mcp_tool_name s23456789012345678901234567 t23456789012345678901234567890)"
[ "${#exact}" -eq 64 ] && [ "$exact" = "mcp__s23456789012345678901234567__t23456789012345678901234567890" ] \
  || fail "a 64-character name is kept ($exact)"

pin_mcp_tools "" crm-main get_tool_catalog learn_tools
[ "$pinned" = "get_tool_catalog mcp__crm_main__get_tool_catalog learn_tools mcp__crm_main__learn_tools" ] \
  || fail "pin_mcp_tools pins the sanitized names ($pinned)"

printf 'OK MCP pin names\n'
