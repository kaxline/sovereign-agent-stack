# Memory

Four places a fact can live. Pick the one that matches the question; do not add a
fifth.

| Store | What it is for | Where it lives |
| --- | --- | --- |
| Built-in `memory` tool | Tiny durable facts injected into every session | `data/hermes/memories/USER.md` and `MEMORY.md` |
| `session_search` | What you said in a past chat | that gateway's `state.db` (FTS, already enabled) |
| Curated files | Long-form notes that will not fit the character cap | `data/memory/` → `/opt/memory` |
| LightRAG | Documents ingested as a knowledge base | the hot `WORKSPACE` (see [Knowledge bases](knowledge-bases.md)) |

This page is the how-to for the curated files and how they sit next to the other
two. Service wiring lives in [Hermes Agent](hermes.md#memory).

## Before you start

You need the Hermes profile running:

```bash
docker compose up -d
```

Curated notes live on the host and are visible to the agent through a bind mount:

| Host path | Container path | Tracked by git |
| --- | --- | --- |
| `data/memory/` | `/opt/memory` | No (`data/` is gitignored) |

`setup.sh` creates the directory, a `README.md`, and empty `INDEX.md` /
`people.md` / `decisions.md` / `preferences.md`. If Docker created `data/memory/`
as root instead, Hermes cannot write there — fix ownership, or set `HERMES_UID`
and `HERMES_GID` in `.env` to your own `id -u` / `id -g`.

Built-in memory files are shared across the default, `api-server`, and `browser`
profiles (compose overlays `data/hermes/memories/` onto each profile home). A
fact saved in the WebUI is the same file the dashboard reads.

`session_search` is **not** shared. Each gateway searches its own `state.db`.

## What belongs where

**Include in curated files** things that should stay true for months and will not
fit in USER.md: people, decisions and what they replaced, standing project
context, preferences that need a paragraph.

**Put in the `memory` tool** (USER.md / MEMORY.md) the handful of facts that
should be in the system prompt every turn: name, timezone, "prefer terse
replies". Hermes caps USER.md around 1375 characters and MEMORY.md around 2200.
When the tool reports the cap, overflow belongs here, not in a longer USER.md.

A standing preference can nudge replies ("prefer terse replies"). It does not
set the assistant's identity or voice. That file is `SOUL.md` on the profile
that serves the chat — see [Conversational tone](hermes.md#conversational-tone-soulmd).
Do not paste writing samples into USER.md.

**Leave in `session_search`** anything that is "what did we say". Copying a
transcript into `notes/` duplicates it and goes stale.

**Leave in LightRAG** documents you ingested as a corpus. Curated memory is not
a second knowledge base.

## Remember

In a Hermes session (WebUI at `http://localhost:8787`, dashboard at
`http://localhost:9119`):

```text
Remember that I prefer terse replies and I am job-searching this quarter.
```

The `working-memory` skill routes the fact. A standing preference also lands in
USER.md via the native `memory` tool. Longer notes land in `data/memory/` and
`INDEX.md` is updated so the next recall can find them.

You can also edit the markdown yourself. Hand-correcting those files is faster
than prompting the agent to "fix" a wrong memory.

Confirm the agent can see the store:

```bash
docker compose exec hermes ls /opt/memory
```

The mount is live, so files you add appear immediately. Changing the skill
itself needs `docker compose restart hermes`; adding a note does not.

## Recall

```text
What do you know about my preferences?
What have we decided about the career search?
```

The agent should read `INDEX.md`, then only the matching file, and name the file
it used. If nothing matches, it should say so rather than guess.

For a past conversation, ask it to search sessions rather than memory files:

```text
Search past sessions for what we said about the career search.
```

That is `session_search`. WebUI chat will not see dashboard/CLI transcripts, and
the other way around.

## Consolidate

Run when `INDEX.md` has drifted from the files on disk, not on every remember:

```text
Consolidate memory.
```

The agent rewrites `INDEX.md` from what is actually there. Empty files stay
listed. It must not invent entries.

## Layout

```text
data/memory/
  README.md         # this file's sibling on disk (not memory)
  INDEX.md          # table of contents; hand-editable
  people.md
  decisions.md
  preferences.md
  notes/            # one topic file per extra note
```

Keep facts you wrote and drafts the agent produced in separate files. Mix them
and the store starts citing its own output back to you as fact.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Skill never loads | Skills are read at startup. Restart with `docker compose restart hermes`, then `docker compose exec hermes hermes skills list \| grep working-memory`. |
| `Path not found` for `/opt/memory` | `HERMES_ENVIRONMENT_HINT` on the `hermes` service no longer lists `/opt/memory`. The environment probe reports `/opt/data` as home, so the agent invents `/opt/data/memory`. |
| Agent cannot write | Host dir owned by root because Docker created it. Re-run `./scripts/setup.sh` or fix ownership. |
| `Write denied` / "read-only" on `/opt/memory` | `HERMES_WRITE_SAFE_ROOT` on `hermes` no longer lists `/opt/memory`. The image default is `/opt/data` only; `write_file` then refuses the mount even when it is rw. |
| WebUI remembers something the dashboard does not | Built-in files should be shared. Check that compose overlays `data/hermes/memories` onto both profile `memories/` dirs (`docs/hermes.md` verification). |
| `session_search` misses a chat you remember | Wrong profile. WebUI is `browser` (`data/hermes/profiles/browser/state.db`); dashboard/CLI is default (`data/hermes/state.db`). |
| USER.md / MEMORY.md stay empty | The local model has to call the `memory` tool. Ask "remember that…" explicitly. Background review only runs every 3 user turns on interactive profiles. |
| Facts from an n8n one-shot landed in USER.md | Unattended `api-server` sessions keep `nudge_interval` at 10 on purpose. Do not lower it. |
