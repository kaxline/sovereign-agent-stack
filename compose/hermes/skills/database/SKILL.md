---
name: database
description: "Query and keep structured data in the stack's shared Postgres through the `sql` MCP tools (execute_sql, list_schemas, get_object_details). Use when the user asks about data an integration has synced (transactions, records, history), wants numbers, totals, or joins across sources, or asks you to keep a table of your own. Do NOT use for notes or documents (files under /opt/memory or /opt/projects) or the knowledge base (LightRAG)."
version: 1.0.0
author: assistant stack (repo-shipped)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [sql, postgres, database, data, tables, analytics]
    category: data
---

# Shared database

The stack runs one Postgres database with **one schema per owner**:

| Schema | Written by | You can |
|---|---|---|
| `agent` | you (dashboard / WebUI sessions only) | read; create, change, and drop your own tables |
| one per integration (e.g. a sync service) | that integration only | read |
| `public` | nobody | read the `data_catalog` view |

Database grants enforce this, not this file. A write outside `agent` fails
with `permission denied`. Unattended API sessions are read-only everywhere.
Do not retry a denied write under another name; tell the user what you were
trying to do.

## Find the data first

Never guess table or column names. Start with the catalog, which lists every
schema, table, and column together with its description:

```sql
SELECT schema_name, table_name, kind, description
FROM data_catalog
WHERE kind <> 'column'
ORDER BY schema_name, table_name;
```

Then the columns of the table you need:

```sql
SELECT column_name, data_type, description
FROM data_catalog
WHERE schema_name = 'some_schema' AND table_name = 'some_table' AND kind = 'column';
```

`list_schemas`, `list_objects`, and `get_object_details` work too, but they do
not show descriptions. Read the descriptions: they say what a table holds, its
units and currency, and which columns are reliable.

## Query well

- Always qualify tables with their schema: `some_schema.some_table`.
- Add `LIMIT` to exploratory queries (50 is plenty). Aggregate in SQL rather
  than pulling raw rows to add up yourself.
- Statements time out after 30 s (read-only) or 60 s (read/write). If a query
  times out, narrow it (date range, filter) instead of retrying it unchanged.
- In read-only sessions some catalog functions such as `obj_description()`
  are rejected; use `data_catalog` instead.
- Report numbers with the query's own filters ("March, checking account
  only"), so the user can tell what was counted.

## Keeping your own tables (read/write sessions)

Create tables only in `agent`, and only when the user asks you to keep
something structured, or a derived result is worth reusing:

```sql
CREATE TABLE agent.reading_list (
  id serial PRIMARY KEY,
  title text NOT NULL,
  added_on date NOT NULL DEFAULT current_date
);
COMMENT ON TABLE agent.reading_list IS 'Books the user wants to read; added on request.';
COMMENT ON COLUMN agent.reading_list.added_on IS 'Date the user asked to add it.';
```

- Always add `COMMENT ON TABLE`, and comment any column whose meaning is not
  obvious. The next session finds your table through those comments.
- Do not copy integration tables into `agent`. Query them where they are, or
  make a view: `CREATE VIEW agent.x AS SELECT ... FROM some_schema.y`.
- Ask before `DROP TABLE` or a `DELETE` / `UPDATE` without a `WHERE`.
- Never put secrets or passwords in a table.
