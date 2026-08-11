# Contributing

Thanks for your interest. This repo ships a Docker Compose **local agent stack** (Hermes by default, with opt-in RAG, automation, and coding profiles).

## Getting started

```bash
git clone <repo-url>
cd assistant
./scripts/setup.sh
make doctor
# Point .env at a model server, or: ./scripts/setup.sh --ollama
docker compose run --rm hermes setup   # first Hermes boot only
make up
```

See [README.md](README.md) and [docs/](docs/README.md).

## Project layout

| Path | Purpose |
|---|---|
| `docker-compose.yml` | Stack definition (profile-gated services) |
| `.env.example` | Documented env template + image pins |
| `scripts/setup.sh` | First-run setup |
| `scripts/doctor.sh` | Preflight checks |
| `docs/` | Per-service guides |
| `compose/` | Build contexts and Hermes bootstrap |
| `n8n/demo-data/` | Starter workflows (`credentials/*.json` is gitignored; setup regenerates it) |
| `opencode/` | OpenCode config template (`opencode.local.json` is gitignored; put your model server there) |
| `searxng/` | SearXNG template (`settings.local.yml` is gitignored) |
| `src/` | **Not shipped** — private WIP, gitignored |
| `data/` | **Not committed** — created by setup |

## Profiles

Default: `COMPOSE_PROFILES=core`. Add with setup flags (`--rag`, `--automation`, `--coding`, `--ollama`) or by editing `.env`.

## Changing secrets

If you rotate `LIGHTRAG_API_KEY`, `NEO4J_PASSWORD`, or `N8N_ENCRYPTION_KEY` after n8n has been initialized:

```bash
export $(grep -E '^(N8N_ENCRYPTION_KEY|LIGHTRAG_API_KEY|NEO4J_PASSWORD|NEO4J_USERNAME)=' .env | xargs)
node scripts/generate-n8n-credentials.js

docker compose run --rm --entrypoint /bin/sh n8n-import -c \
  'n8n import:credentials --separate --input=/demo-data/credentials'
```

## Pull requests

- Keep changes focused; match existing compose and shell style.
- Do not commit secrets or local overlays listed in `.gitignore`.
- Update README / `docs/` when you add env vars or services.
- CI runs `docker compose config` across profile combos, shellcheck, and hadolint.

## First public history

If you are cutting the first GitHub release from a finished working tree, follow [docs/releasing.md](docs/releasing.md) so the commits tell the engineering story instead of collapsing into one squash.
