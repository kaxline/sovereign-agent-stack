---
name: living-todos
description: "Manage the user's durable, cross-session todo / to-do / task list. Use for 'add to the todo list', 'what's on our todo list', 'mark X done', priorities, or any request to persist tasks across chats. Uses Vikunja (tasks_list, tasks_create) when those tools are available, otherwise a markdown file. Do NOT use the session-only `todo` tool for these."
version: 1.1.0
author: assistant stack (repo-shipped)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [todo, todos, tasks, priorities, productivity, vikunja]
    category: productivity
    config:
      - key: todos.list_path
        description: Markdown file holding the living cross-session todo list
        default: "/opt/projects/project-todo-list/README.md"
        prompt: Path to the durable todo list markdown file
---

# Living todos

The user's **todo list is durable** and must survive new WebUI sessions.
The Hermes built-in `todo` tool is an in-memory scratchpad for the *current*
chat only — never use it for "add/list/complete my todos".

## Pick the backend first

- **Vikunja** — when the Vikunja MCP tools are in your tool list
  (`tasks_list`, `tasks_create`, `projects_list`, possibly named
  `mcp__vikunja__tasks_list` and so on). The list lives there; do not also
  write the markdown file. See [Vikunja](#vikunja) below.
- **Markdown file** — otherwise. The file is `todos.list_path` (default
  `/opt/projects/project-todo-list/README.md`). Use the resolved config value
  when present. See [Markdown file](#markdown-file) below.

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

## Vikunja

Tasks belong to projects. Every account starts with an `Inbox` project; use it
unless the user names another one.

### List

1. `tasks_list` with `filter: "done = false"`, `sort_by: ["due_date", "priority"]`
   and `order_by: ["asc", "desc"]`. Add `format: "markdown"` if you will quote
   descriptions.
2. Summarize open items, grouped by project when there is more than one.
   Mention due dates and high priorities. If there are none, say the list is
   empty; do not invent tasks.

Filters use Vikunja's filter language: `done = false && priority >= 3`,
`due_date < now+7d`, `title like '%dentist%'`, `project = 4` (a numeric id from
`projects_list`, not a name). See https://vikunja.io/docs/filters.

### Add

1. `projects_list` once to get the project id (Inbox, or the one the user named).
   Do not create a project unless the user asks for one.
2. Check `tasks_list` with `filter: "done = false && title like '%<word>%'"`
   first, so you do not add a duplicate of an open task.
3. `tasks_create` with `project` and `title`. Set `due_date` (RFC 3339, e.g.
   `2026-10-20T17:00:00Z`) and `priority` (1 low, 2 medium, 3 high, 4 urgent,
   5 do now) only when the user gave them. Put longer notes in `description`
   with `format: "markdown"`.
4. Confirm the title, project, and any due date you set.

### Complete / change

1. Find the task's numeric id with `tasks_list` (`title like '%<word>%'`).
2. `tasks_update` with `task: <id>` and only the fields that change, e.g.
   `done: true`. Fields you leave out stay as they are.
3. Ask before `tasks_delete`; marking done is usually what the user means by
   "remove".

### Read-only sessions

Unattended API sessions get a read-only token: `tasks_create` and
`tasks_update` are not in the tool list there. Say the list could not be
changed from this session instead of falling back to the markdown file.

### Other tools

Labels, comments, and assignees have their own tools (`labels_*`,
`task_comments_*`, `task_assignees_*`). Anything else allowed for this token is
reachable through `find_action` then `do_action`.

## Markdown file

### List

1. `read_file` on `<list_path>`.
2. Summarize open items by priority section. Quote the path you used
   (`data/projects/project-todo-list/README.md` on the host).
3. If the file is missing, say so and offer to create it — do not invent tasks
   and do not fall back to the session `todo` tool.

### Add

1. `read_file` on `<list_path>` first.
2. Insert the new item under the right priority section as a markdown checkbox
   (`- [ ] …`). Prefer High/Critical when the user says it is urgent; otherwise
   Medium. Do not duplicate an existing open item.
3. Write back with `write_file` or `patch`. Confirm the path and the new line.
4. Never call the session `todo` tool for this.

### Complete / remove

1. Read the file.
2. Mark the matching item `- [x]` or remove it if the user asked to delete.
3. Write back. Confirm what changed.

## Moving the markdown list into Vikunja

Only when the user asks. Read the file, create one Vikunja task per open
`- [ ]` item (Critical → priority 4, High → 3, Medium → 2, Low → 1), then
tell the user how many were created and leave the file in place for them to
archive.

## Pitfalls

- **Session `todo` tool.** Empty in every new chat. Using it is the bug this
  skill exists to prevent.
- **Two lists.** With Vikunja available, do not also write the markdown file;
  the user would end up with two lists that disagree.
- **Wrong path.** Not `/opt/data/projects/…`, not `yachtcopter/TODO.md`, not
  `PROJECT_TODO_LIST.md` unless that file exists. Only `<list_path>` under
  `/opt/projects/project-todo-list/`.
- **Claiming a write without calling a tool.** If `tasks_create` /
  `tasks_update` / `write_file` / `patch` did not run or returned an error, the
  list was not updated — say so.
