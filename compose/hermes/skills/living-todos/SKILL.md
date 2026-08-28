---
name: living-todos
description: "Manage the user's durable, cross-session todo / to-do / task list. Use for 'add to the todo list', 'what's on our todo list', 'mark X done', priorities, or any request to persist tasks across chats. Do NOT use the session-only `todo` tool for these."
version: 1.0.0
author: assistant stack (repo-shipped)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [todo, todos, tasks, priorities, productivity]
    category: productivity
    config:
      - key: todos.list_path
        description: Markdown file holding the living cross-session todo list
        default: "/opt/projects/project-todo-list/README.md"
        prompt: Path to the durable todo list markdown file
---

# Living todos

The user's **todo list lives on disk** and must survive new WebUI sessions.
The Hermes built-in `todo` tool is an in-memory scratchpad for the *current*
chat only — never use it for "add/list/complete my todos".

The list file is `todos.list_path` (default
`/opt/projects/project-todo-list/README.md`). Use the resolved config value
when present.

## When to use this skill

- "Add … to the todo list" / "put this on my todos"
- "What's on our todo list?" / "what should I work on?"
- "Mark X done" / "complete …" / "remove … from the list"
- Priorities, this week / this month tasks that should still be there tomorrow

Do **not** use this skill for:

- One-off multi-step *planning inside the current reply* that the user did not
  ask to keep (decompose a coding task, then discard) — that is not their list
- Durable facts about the user — that is `working-memory` / the `memory` tool
- Past chat recall — that is `session_search`

## Operation 1: List

1. `read_file` on `<list_path>`.
2. Summarize open items by priority section. Quote the path you used
   (`data/projects/project-todo-list/README.md` on the host).
3. If the file is missing, say so and offer to create it — do not invent tasks
   and do not fall back to the session `todo` tool.

## Operation 2: Add

1. `read_file` on `<list_path>` first.
2. Insert the new item under the right priority section as a markdown checkbox
   (`- [ ] …`). Prefer High/Critical when the user says it is urgent; otherwise
   Medium. Do not duplicate an existing open item.
3. Write back with `write_file` or `patch`. Confirm the path and the new line.
4. Never call the session `todo` tool for this.

## Operation 3: Complete / remove

1. Read the file.
2. Mark the matching item `- [x]` or remove it if the user asked to delete.
3. Write back. Confirm what changed.

## Pitfalls

- **Session `todo` tool.** Empty in every new chat. Using it is the bug this
  skill exists to prevent.
- **Wrong path.** Not `/opt/data/projects/…`, not `yachtcopter/TODO.md`, not
  `PROJECT_TODO_LIST.md` unless that file exists. Only `<list_path>` under
  `/opt/projects/project-todo-list/`.
- **Claiming a write without calling a tool.** If `write_file`/`patch` did not
  run, the list was not updated — say so.
