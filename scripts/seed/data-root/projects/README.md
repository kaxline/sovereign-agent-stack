# Projects

Each subdirectory here is one project: a working directory the agent reads from
and drafts into. Hermes reads them at `/opt/projects`. Git tracks none of it.

Use this for work that needs its own reference material and generated output
side by side, like a job search, a book, a client engagement, or a research topic.

## Setup

Scaffold a project from the repo template (from the assistant repo root):

    make project-init PROJECT=my-project

Or create the directory yourself and copy reference files in. Either way,
give the project an `AGENTS.md` brief and keep source notes apart from drafts.

Then either:

- Select `/opt/projects/my-project` in the **WebUI** workspace dropdown
  (library folders appear automatically; brief is injected when selected), or
- Name the path in a prompt: `Using the notes in /opt/projects/my-project, …`

The mount is live, so files you add appear immediately. No Hermes restart
required.

## Conventions that help small models

| File | Role |
| --- | --- |
| `AGENTS.md` | Short always-on brief while this project is the WebUI workspace |
| `sources.yaml` | Extra host directories to index (optional; default is this folder) |
| `INDEX.md` | One-line map of source files; refreshes itself (`make project-index PROJECT=my-project` refreshes it now) |
| Source notes | Put a `description:` field in YAML frontmatter for a better index |
| `drafts/` / `applications/` / `artifacts/` | Generated output; skipped by the indexer |

## Keep source material and generated output apart

Put reference material you wrote and drafts the agent produced in separate
subdirectories. Mix them and the corpus starts citing its own output back to
you as fact. Keeping them apart also lets you ingest only the source half into
LightRAG later, once a project outgrows reading files directly.

## Notes

- Requires the Hermes `core` profile.
- Agents can write here. Review generated files before treating them as fact.
- Full convention: `docs/projects.md` in the assistant repo.
- Relocate with `make data-dir-migrate` — see `docs/data-dir.md`.
