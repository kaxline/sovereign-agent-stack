"""Short tool names models use for the stack's MCP tools (stack-only).

patch-tool-call-flat-args.py maps these names in ``tool_call`` before validation,
and tool_eval_trace.py maps them when it records a turn, so eval cases score
against the MCP names. Both overlays install this file as
/opt/hermes/tools/stack_tool_aliases.py.
"""

STACK_TOOL_ALIASES = {
    "web_search": "mcp__searxng__searxng_web_search",
    "web_extract": "mcp__searxng__web_url_read",
    "searxng_web_search": "mcp__searxng__searxng_web_search",
    "web_url_read": "mcp__searxng__web_url_read",
    "coding_list_roots": "mcp__opencode__coding_list_roots",
    "coding_start_task": "mcp__opencode__coding_start_task",
    "coding_get_task_status": "mcp__opencode__coding_get_task_status",
    "coding_wait_for_task": "mcp__opencode__coding_wait_for_task",
    "coding_get_task_result": "mcp__opencode__coding_get_task_result",
    "coding_continue_task": "mcp__opencode__coding_continue_task",
}
