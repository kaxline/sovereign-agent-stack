# Project brief

Keep this file small (roughly 1.5–2k tokens). The WebUI injects it as a system
message on every turn while this project is the selected workspace. CLI and
dashboard can also load it natively when the working directory is this project.

## Positioning

One or two sentences: what this body of work is for, and what “good” looks like.

## Key facts

- Fact the model should never invent around
- Another durable fact (metrics, constraints, names)

## How to retrieve

1. Read `INDEX.md` for a one-line map of source files (grouped by source id).
2. Open matching files using the path **exactly as written**. Do not prefix
   `/opt/projects` onto paths that are already absolute.
3. Extra source roots are read-only notes; write drafts under this project.
4. Prefer source notes over anything under `drafts/`.
5. If `INDEX.md` looks wrong or incomplete, list the project directory and read
   files directly. Extra roots need `make context-root-add` plus a Hermes recreate
   before they are visible.

## Out of scope

What this project is *not* for (so the model does not stretch the brief).
