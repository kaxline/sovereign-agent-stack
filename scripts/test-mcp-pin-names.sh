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

pin_mcp_tools "" crm-main get_tool_catalog learn_tools
[ "$pinned" = "get_tool_catalog mcp__crm_main__get_tool_catalog learn_tools mcp__crm_main__learn_tools" ] \
  || fail "pin_mcp_tools pins the sanitized names ($pinned)"

printf 'OK MCP pin names\n'
