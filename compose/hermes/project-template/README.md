# Project

Working directory for one body of work. Hermes sees it at
`/opt/projects/<name>`. The outer repo does not track this tree.

## Layout

| Path | Role |
|---|---|
| `AGENTS.md` | Short brief injected when this project is the WebUI workspace |
| `INDEX.md` | File map — regenerate with `make project-index` |
| `notes/` | Source material you wrote |
| `drafts/` | Agent-generated output (never treat as evidence) |

Keep source and drafts apart. Mix them and later retrieval cites machine-written
claims as fact.

## Setup

1. Fill in `AGENTS.md` (positioning, key facts, retrieval rules).
2. Add source files under `notes/` with optional frontmatter:

   ```yaml
   ---
   description: One line for INDEX.md
   ---
   ```

3. Regenerate the index:

   ```bash
   make project-index PROJECT=<name>
   ```

4. In Hermes WebUI, add a workspace pointing at `/opt/projects/<name>`.

See `docs/projects.md` in the assistant repo.
