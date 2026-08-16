---
name: working-memory
description: "Remember and recall durable facts about the user, people, decisions, and preferences. Use for 'remember that…', 'what do you know about…', 'consolidate memory', or any request to store or retrieve long-form notes that should persist across sessions."
version: 1.0.0
author: assistant stack (repo-shipped)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [memory, recall, notes, preferences, people, decisions]
    category: productivity
    config:
      - key: memory.store_path
        description: Directory holding curated long-form memory files
        default: "/opt/memory"
        prompt: Path to the curated memory directory
---

# Working memory

Keep durable facts the user wants remembered across sessions. This is a *routing*
skill: pick the right store, write as little as will stay true, and never invent.

The store root is `memory.store_path` (default `/opt/memory`). Its resolved value
is injected when this skill loads — use that value, not a hardcoded path.

## When to use this skill

- "Remember that…" / "don't forget…" / "save this for later"
- "What do you know about X?" / "what have we decided about…"
- "Consolidate memory" / "rebuild the memory index"
- Any request to persist a preference, person, decision, or project fact

Do **not** use this skill for:

- Documents and knowledge-base lookup — that is LightRAG (`query_document`)
- "What did we talk about last Tuesday?" — that is `session_search`
- Writing in the user's prose style — that is `write-in-voice`
- Project working files (drafts, source material) — that is `/opt/projects`

## Route first

| Kind of fact | Store | Why |
|---|---|---|
| Tiny durable facts (name, timezone, a standing preference) | Native `memory` tool → `USER.md` / `MEMORY.md` | Injected into every session; cap is ~1375 / ~2200 characters |
| What was said in a past chat | `session_search` | FTS over that gateway's `state.db`; do not grep sqlite yourself |
| Documents, notes ingested as a corpus | LightRAG `query_document` | GraphRAG over the hot `WORKSPACE` |
| Everything that will not fit the cap | Files under `<store_path>` | User-owned, hand-editable markdown |

A preference or identity fact that also belongs in the prompt-injected profile
gets **both**: write the file *and* call `memory(target="user")`. Do not duplicate
the same paragraph into three stores.

Each Hermes gateway searches **its own** `state.db`. WebUI chat is the `browser`
profile; dashboard/CLI is the default profile; n8n is `api-server`. If
`session_search` finds nothing, say which profile you searched rather than
claiming the conversation never happened.

## Layout

```text
<store_path>/
  README.md          # user-facing instructions (not memory)
  INDEX.md           # distilled table of contents; hand-editable
  people.md
  decisions.md
  preferences.md
  notes/             # one topic file per extra note
```

Ignore `README.md` and dotfiles when listing memory. Do not dump the whole tree
into a reply.

Keep facts the user wrote and drafts you produced in separate files. Mix them
and you will cite your own output back as fact.

## Operation 1: Remember

1. Classify the fact against the routing table above.
2. If it belongs in the native `memory` tool, call it. Stop if that is the whole
   request.
3. Read `<store_path>/INDEX.md`. Pick the file (`people.md`, `decisions.md`,
   `preferences.md`, or a new `notes/<topic>.md`).
4. Read that file. **Edit in place** — add or replace the relevant section; do
   not append a duplicate. For a decision, record what it replaces when you know.
5. Update `INDEX.md` so the new or changed section is findable. One line is
   enough.
6. If the fact is a preference or identity item, also call `memory(target="user")`.
7. Tell the user which file you wrote (host path `data/memory/…`) and that it is
   hand-editable. Do not dump the file back.

If `<store_path>` is missing or not writable, stop and say so. The host directory
is `data/memory/`; if Docker created it as root, `setup.sh` did not run first.

## Operation 2: Recall

1. Read `<store_path>/INDEX.md`.
2. Open only the matching file(s). Do not read every note "just in case".
3. Answer from those files. Quote the file you used. If nothing matches, say so
   rather than guessing, then offer `session_search` or LightRAG when those
   stores are the better fit.
4. If INDEX is stale relative to files on disk, mention it and offer to
   consolidate.

## Operation 3: Consolidate

Run on request, not on every remember. This is the expensive pass.

1. List `<store_path>/` and `<store_path>/notes/`. Skip `README.md` and dotfiles.
2. Read each remaining file.
3. Rewrite `INDEX.md` as a table of contents of what is actually there. Empty
   files stay listed with nothing under them. **Never invent.**
4. If two files repeat the same fact, say so and ask which to keep. Do not
   silently merge.
5. Tell the user `INDEX.md` is hand-editable.

## Pitfalls

- **Writing past-chat recall into files.** `session_search` already indexes
  transcripts. Copying a conversation into `notes/` duplicates it and goes stale.
- **Stuffing USER.md.** When the native `memory` tool reports the cap, overflow
  belongs here, not in a longer USER.md.
- **Citing generated drafts as fact.** Files you wrote under `notes/` are drafts
  until the user says otherwise.
- **Inventing from an empty store.** Empty sections mean unknown, not "nothing
  to see, so improvise".
- **Wrong `state.db`.** `session_search` does not see other profiles. Say so.
