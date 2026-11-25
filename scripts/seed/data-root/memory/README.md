# Curated memory

Long-form notes Hermes should remember across sessions, when they will not
fit in the short USER.md / MEMORY.md files. Hermes reads this directory at
`/opt/memory`. Git tracks none of it.

## Setup

The files are already here. Fill them in yourself, or ask Hermes:

    Remember that I prefer terse replies and I am job-searching this quarter.
    What do you know about my preferences?
    Consolidate memory.

Tiny durable facts (name, timezone, a standing preference) also go into
Hermes' built-in `memory` tool, which injects USER.md every session. This
directory is for everything that outgrows that cap.

## Layout

    memory/
      README.md         # this file (not memory)
      INDEX.md          # table of contents; hand-editable
      people.md
      decisions.md
      preferences.md
      notes/            # one topic file per extra note

## Keep source notes and generated drafts apart

Put facts you wrote and drafts the agent produced in separate files. Mix them
and the store starts citing its own output back to you as fact.

## Notes

- Requires the Hermes profile. For the routing (built-in vs files vs past
  chats vs LightRAG), see `docs/memory.md`.
- Agents can write here. Review generated files before treating them as fact.
- Relocate with `make data-dir-migrate` — see `docs/data-dir.md`.
