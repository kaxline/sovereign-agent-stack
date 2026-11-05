---
name: crm
description: "Look up and update the user's CRM (Twenty): people, companies, opportunities, notes, tasks. Use for 'who is X at Y', 'add Z as a contact', 'log a note on the Acme deal', 'what opportunities are open', or anything about contacts, leads, deals or accounts. Uses the crm-<workspace> MCP tools (mcp__crm-main__execute_tool and so on)."
version: 1.0.0
author: assistant stack (repo-shipped)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [crm, twenty, contacts, companies, deals, sales]
    category: productivity
---

# CRM (Twenty)

Each CRM workspace is its own MCP server named `crm-<workspace>`: `crm-main`
for the usual one, `crm-sales` or `crm-hosted` when the user has several.
If none of these tools are in your list, the CRM isn't set up; say so rather
than keeping notes elsewhere.

## Pick the workspace

Use the one the user names ("my sales CRM" → `crm-sales`). With one
workspace, use it. With several and no name, ask which one before writing;
for a lookup, search each and say where you found it.

## Find, learn, call

The seven tools are the same on every workspace. Records are reached through
three of them:

1. `get_tool_catalog` lists the record tools by category (`find_many_people`,
   `create_one_company`, `update_one_opportunity`, ...).
2. `learn_tools` with the names you need returns their input schemas. Read
   them before the first call; field shapes are nested (`name.firstName`,
   `emails.primaryEmail`).
3. `execute_tool` with `toolName` and `arguments` runs one.

`find_many_*` fails without `select`: pass the fields you want, for example
`"select": ["id", "name", "emails"]`, plus a `filter` and a small `limit`.
Search before creating, so you don't add a person or company twice.

`list_object_metadata_names` names the objects in this workspace, custom
ones included. `search_help_center` answers how-to questions about Twenty itself.

## Unattended runs

On scheduled and API runs the key is read-only: create and update tools are
missing from the catalog. Report what you found and what you would change;
don't retry a write through another tool.
