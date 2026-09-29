---
name: delegate-coding
description: "REQUIRED for implement/refactor/fix/test under a coding root, and for locate/diagnose of a named repo (look in X, find an error string). Mutating work: coding_start_task. Locate: coding_list_roots then search the host path — never invent /opt/projects/<name>."
version: 1.2.0
author: assistant stack (repo-shipped)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [coding, code, refactor, tests, implement, locate, diagnose, opencode, required]
    category: development
---

# Delegate coding (mandatory)

When the user asks to implement, refactor, fix, or test code under a **coding
root**, you MUST delegate with the OpenCode MCP tools. Doing the edits yourself
is a routing failure even if the end result looks correct.

When they ask to **look in** a named repo, **find** an error string, or
**diagnose** why a message appears in a git repo, resolve the host path first
(`coding_list_roots`). Do not invent `/opt/projects/<name>`.

## Hard rules

1. **Call coding tools directly by their own tool name** (preferred). In the
   tool list look for `mcp__opencode__coding_start_task` (or `coding_start_task`)
   and invoke that function **as a top-level tool call**, the same way you call
   `read_file`. Do **not** wrap it in `tool_call({calls: ...})` when the tool
   is already visible.
2. If you must use the `tool_call` bridge, pass `calls` as a real JSON **array**
   of objects, never as a string. Wrong: `"calls": "[{...}]"`. Right:
   `"calls": [{"name":"mcp__opencode__coding_start_task","arguments":{...}}]`.
3. **Do not stop after `tool_describe`.** If you describe a coding tool, the
   next action must invoke `coding_start_task` (directly or via a correctly
   shaped `tool_call`). Saying “dispatching now” and ending the turn is a failure.
4. **First mutating action** for an implement/fix/test request under a coding
   root: **`coding_start_task`** (optional `coding_list_roots` first).
5. **Forbidden** on coding-root source while a coding task is the goal:
   `write_file`, `patch`, `terminal` (to edit files or run ad-hoc fix loops),
   `execute_code`. Brief `read_file` / list is OK, then you must call
   `coding_start_task`.
6. After `coding_start_task`, use only wait/status/result/continue until done,
   then summarize. If coding tools are missing from your tool list, say so —
   do not fall back to hand-editing.

## When this skill applies

- Implement a feature or function in a repository under a coding root
- Refactor, rename, or reorganize source files there
- Add or fix unit/integration tests and run them there
- Bug fixes that touch source code there
- Locate / diagnose: “look in \<repo\>”, find an error string, or explain why
  a message appears in a named git repo

## When not to use

- Short prose / notes under `/opt/projects` — edit those files directly
- Memory, calendars, research, web search
- Pure questions about files the user already pointed at with a real path
  (read-only is fine). A bare repo name is **not** that — resolve it with
  `coding_list_roots` first. The moment edits or test runs are required,
  switch to `coding_*`

## Coding roots

Call `coding_list_roots` if unsure. Paths are host absolute paths (for example
`/Users/you/code/my-app`). Do not invent `/opt/...` aliases for coding roots.

## Locate / diagnose (read-only)

1. If the path is not already a known host absolute path, call
   `coding_list_roots`. Use a child of a coding root — do **not** invent
   `/opt/projects/<name>` when that slug is not an immediate child of
   `/opt/projects`.
2. Search **that** host path only (`search_files` / `read_file` / a brief
   `terminal`). If `search_files` returns Path not found plus similar paths,
   use those paths or `clarify` once; do not retry the same missing path.
3. After two or three misses, answer or `clarify`. Do not start reading
   Hermes internals (`/opt/hermes`, `/opt/data`, container runtime) unless
   the user is debugging Hermes.
4. The moment the user wants a fix or tests, switch to the happy path below.

## Happy path

1. `coding_start_task(directory="<abs-repo>", prompt="<self-contained task>")`
   — include “run tests until they pass” when tests matter.
2. `coding_wait_for_task(task_id=...)` (repeat if `timed_out`).
3. `coding_get_task_result(task_id=...)` and summarize what changed.
4. Follow-ups: `coding_continue_task(task_id=..., prompt=...)`.

## Access outside coding roots

If `coding_start_task` returns `needs_access: true`:

1. Ask the user to grant access (plain language; no Docker jargon unless asked).
2. After they confirm, give: `make coding-root-add DIR=<absolute-path>`
3. Tell them to recreate hermes/opencode/opencode-mcp, then retry `coding_start_task`.

## Reporting

- Prefer the coding agent’s summary/diff over re-doing the work.
- Do not send the user to another UI for routine coding.
