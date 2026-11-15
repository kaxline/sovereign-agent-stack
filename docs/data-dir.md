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
| `DB_DATA_DIR` (default `./data/db`) | Shared Postgres cluster, `db` profile. Not moved by `data-dir-migrate`; see [database.md](database.md#storage) |
| `OPENCODE_WORKSPACE_HOST` | Primary coding root for OpenCode + Hermes (see [opencode.md](opencode.md)) |
| `CONTEXT_EXTRA_ROOTS` | Extra Hermes read-only notes mounts (see [projects.md](projects.md#extra-source-directories)) |
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

## Backups

`scripts/backup.sh` writes `./backups/<UTC timestamp>-<version>/` (set
`BACKUP_DIR` or pass `--dest DIR` to put it elsewhere), with one `.tar.gz` per
part and a `manifest.json` listing them:

| Part | What |
| --- | --- |
| `stack` | the stack directory without `data/`, `backups/` and `.git`: code, `.env`, the override file and the other local configs |
| `data` | `./data`, without `*.bak-*` folders, `backups/`, `worktree-backups/` and the host mail and Messages indexes (`mail/`, `messages/`, `messages-events/`), which the host helpers rebuild |
| `data-root` | `ASSISTANT_DATA_ROOT`, when it is not `./data` |
| `dir-KEY` | each `*DATA_DIR` in `.env` that points outside the stack and both roots, such as `SIGNAL_CLI_DATA_DIR` |
| `volume-NAME` | each named volume in `docker compose --profile '*' config` that exists, so a disabled add-on's volume is kept too. Left out: the caches (`hermes_uv_cache`, `actual_mcp_ro_cache`, `actual_mcp_rw_cache`), `neo4j_logs`, `bitwarden_signin`, and `email_index` and `search_gate_data`, which hold mail and agents' search history and can be rebuilt. `ollama_data` (downloaded models) is included and can be large |

The Postgres of the `db` add-on, Vikunja and Twenty, and Hermes' SQLite files,
live under `data/`, and a copy of them is consistent only with the stack
stopped. `backup.sh` refuses while any service runs; `--stop` stops the stack
and starts the same services again afterwards. `--plan` lists the parts
without writing anything. tar runs as root in the pinned Hermes image, so it
can read files the database containers own on Linux, and keeps their owners.

A backup holds every secret in the stack. The folder is created with mode 700;
keep copies as private as `.env`.

```bash
scripts/backup.sh --stop
scripts/backup.sh restore --from backups/20261115T171503Z-v0.3.17 --stop
```

`restore` unpacks `data` into `./data` and `data-root` into the current
`ASSISTANT_DATA_ROOT`, wherever the stack was when the backup was made.
`dir-KEY` parts, which sit outside the stack (such as signal-cli's store, where
an older copy can break a linked device's sessions), are skipped unless you
pass `--with-dirs`, and then only while `.env` still names the same path. A
backup stored inside a directory restore would replace is refused; move it out
first. Each directory is moved to `PATH.pre-restore-<timestamp>` before
unpacking, then what the backup left out (mail indexes, older backups) is moved
back. It saves each volume's current contents under
`<backup>/pre-restore-<timestamp>/` before emptying it. Nothing is deleted;
remove the `.pre-restore-*` copies once the restored stack works. The stack
stays stopped afterwards. `stack.tar.gz` is not restored; unpack it by hand
if you need an old `.env` or config. `--json` on either command prints
[JSON lines](extending.md#json-lines) in `addon.sh`'s shapes: a `setup` line
per part, then `done` (with `path` on `create`) or `error`.

## `make clean`

`make clean` runs `docker compose down -v` and removes the **repo** `./data/`
tree (including Hermes state). If `ASSISTANT_DATA_ROOT` resolves **outside** the
repo, that directory is left alone. If it still points at `./data`, user content
under it is deleted with the tree — migrate out first.

## Related

- [Projects](projects.md) — `AGENTS.md`, `sources.yaml`, INDEX, context roots
- [Memory](memory.md) — curated notes vs Hermes USER.md
- [Writing voice](writing-voice.md) — voice corpora
- [Knowledge bases](knowledge-bases.md) — inputs / rag_storage / corpora
