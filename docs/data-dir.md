# User data directory

User-authored content (projects, writing voices, curated memory, LightRAG
inputs/storage, corpus registry) lives under **`ASSISTANT_DATA_ROOT`**. Hermes
agent state stays at **`./data/hermes`** inside the repo.

That split means you can wipe or delete the compose stack (including `make clean`)
without destroying projects and notes that live outside the repo.

## What lives where

| Path | Contents |
|---|---|
| `$ASSISTANT_DATA_ROOT/projects/` | Per-project working dirs → `/opt/projects` |
| `$ASSISTANT_DATA_ROOT/voice/` | Writing-voice corpora → `/opt/voice` |
| `$ASSISTANT_DATA_ROOT/memory/` | Curated long-form notes → `/opt/memory` |
| `$ASSISTANT_DATA_ROOT/inputs/` | LightRAG / n8n document drop folders |
| `$ASSISTANT_DATA_ROOT/rag_storage/` | LightRAG vector/KV persistence |
| `$ASSISTANT_DATA_ROOT/corpora/` | Corpus registry (`registry.json`) |
| `./data/hermes/` | Hermes config, sessions, USER.md / MEMORY.md, WebUI state |
| `OPENCODE_WORKSPACE_HOST` | OpenCode code mount (separate; see [opencode.md](opencode.md)) |
| `SIGNAL_CLI_DATA_DIR` | Signal session store (separate; see [signal.md](signal.md)) |

Default `ASSISTANT_DATA_ROOT` is `./data` (same tree as Hermes state, for
backward compatibility). Prefer an absolute path outside the clone once you
have real work to keep.

## Commands

```bash
make data-dir-show
make data-dir-set DIR=~/AssistantData
make down
make data-dir-migrate DIR=~/AssistantData
# After smoke-test:
make data-dir-migrate FROM=./data DIR=~/AssistantData REMOVE_SOURCE=1
make up
```

Or call the script directly:

```bash
./scripts/data-dir.sh show
./scripts/data-dir.sh set ~/AssistantData
./scripts/data-dir.sh migrate ~/AssistantData
./scripts/data-dir.sh migrate ./data ~/AssistantData --remove-source
```

| Subcommand | Behavior |
|---|---|
| `show` | Resolved absolute root, subdir status, hermes reminder |
| `set <path>` | Write `ASSISTANT_DATA_ROOT`, create layout, **no file move** |
| `migrate <to>` | Copy the six user dirs from the current root → `<to>`, then retarget |
| `migrate <from> <to>` | Same with an explicit source |

`migrate` refuses while hermes / hermes-webui / lightrag / n8n / gpt-researcher
are running unless you pass `--force` (prefer `make down` first). It never
copies or deletes `./data/hermes`.

Setup also accepts `--data-root PATH` so a fresh install never starts under
`./data` for user content.

## After changing the root

Compose bind mounts are fixed at container create time:

```bash
make down && make up
```

Container paths stay the same (`/opt/projects`, `/opt/voice`, …). Only the host
side of the bind changes.

## `make clean`

`make clean` runs `docker compose down -v` and removes the **repo** `./data/`
tree (including Hermes state). If `ASSISTANT_DATA_ROOT` resolves **outside** the
repo, that directory is left alone. If it still points at `./data`, user content
under it is deleted with the tree — migrate out first.

## Related

- [Projects](projects.md) — `AGENTS.md`, INDEX, WebUI workspaces
- [Memory](memory.md) — curated notes vs Hermes USER.md
- [Writing voice](writing-voice.md) — voice corpora
- [Knowledge bases](knowledge-bases.md) — inputs / rag_storage / corpora
