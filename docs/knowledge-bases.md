# Knowledge bases (corpora)

LightRAG + Neo4j give you GraphRAG over markdown (and other docs). This stack keeps **one hot corpus** at a time (`WORKSPACE` in `.env`). You can keep **dozens of cold corpora** on disk and switch with a CLI.

Requires the **`rag`** compose profile (`./scripts/setup.sh --rag`).

## Quick path

```bash
# Create a cold corpus (dirs + registry row; does not switch)
make corpus-create SLUG=demo-a

# Drop files into the inputs folder
cp ~/notes/*.md data/inputs/demo-a/

# Make it hot (rewrites WORKSPACE, recreates LightRAG; recreates Hermes if slug changed)
make corpus-use SLUG=demo-a

# Ingest whatever is under data/inputs/<active>/
make corpus-ingest

# Chat: LightRAG WebUI, or Hermes with "Use LightRAG to …"
# http://localhost:9621/webui/
```

Same commands via `./scripts/corpus.sh <create|use|list|ingest|destroy> …`.

## Layout

| Path | Role |
|---|---|
| `data/inputs/<slug>/` | Drop zone for source files (MD, PDF, DOCX, TXT, …) |
| `data/rag_storage/<slug>/` | LightRAG vectors / KV for that workspace |
| `data/corpora/registry.json` | Slug metadata (embedding model/dim, created_at) |
| `.env` `WORKSPACE` | **Active** corpus LightRAG is serving |

Neo4j stays shared. LightRAG isolates workspaces with a per-slug node label plus the storage dirs above. Do **not** set `NEO4J_WORKSPACE` or `POSTGRES_WORKSPACE` in `.env` — those overrides collapse every corpus into one namespace.

## Commands

| Make target | Behavior |
|---|---|
| `make corpus-create SLUG=…` | Create dirs + registry; seed `getting-started.md`; **no** switch |
| `make corpus-use SLUG=…` | Set hot `WORKSPACE`; recreate `lightrag` + `lightrag-mcp`; recreate `hermes` only when the slug changes |
| `make corpus-list` | List corpora; `*` marks active |
| `make corpus-ingest` | `POST /documents/scan` for the **active** corpus (optional `SLUG=` must match active) |
| `make corpus-destroy SLUG=…` | Delete dirs + Neo4j label nodes + registry row; **refuses** if active |

Existing installs: the first `list`/`create` **backfills** the current `WORKSPACE` into the registry if `data/inputs/<WORKSPACE>/` already exists.

## Switching

Only one corpus is queryable at a time. Switching is intentional and a few seconds:

1. `make corpus-use SLUG=other`
2. Wait for LightRAG health (the script polls `/health`)
3. Hermes’ `HERMES_ENVIRONMENT_HINT` names the new `WORKSPACE` so KB-first prompting stays accurate

There is no cross-corpus query. n8n’s LightRAG scan/query workflows always hit whichever workspace is currently hot.

## Chat

- **LightRAG WebUI** (`http://localhost:9621/webui/`): queries only the hot corpus.
- **Hermes**: with `LIGHTRAG_MCP_ENABLED=1`, ask it to use LightRAG (e.g. “Use LightRAG to summarize my knowledge base”). Prefer `query_document` before web search; if the KB is empty, say so rather than inventing. SearXNG remains available when you want broader context.

## Embedding dimension

`EMBEDDING_DIM` in `.env` must match the embedding model **and** the dim recorded for that corpus in the registry. Changing dim against an existing store silently corrupts vectors. `corpus use` and `make doctor` refuse or warn on mismatch.

## Destroy

```bash
make corpus-use SLUG=keep-me   # switch away first
make corpus-destroy SLUG=old-corpus
# type the slug to confirm
```

## Related

- [n8n workflows](n8n.md) — scan/query HTTP patterns against the hot workspace
- [Hermes Agent](hermes.md) — LightRAG MCP allowlist and “use LightRAG…” prompting
- [Hermes WebUI](hermes-webui.md) — chat UI
