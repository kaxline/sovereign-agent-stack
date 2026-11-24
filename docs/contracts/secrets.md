# Secrets

Service configuration that must stay private: database passwords, encryption keys, API keys
the stack's own services use. Agent logins are a different category; see
[credentials](credentials.md).

There is no secret-source contract yet. Each consumer reads its secrets from a file or an env
var that setup or an add-on hook writes, and nothing outside the stack fills them in. The stack
secrets sync, which filled some of these files from Bitwarden, was removed in 0.3.14 because no
install used it. Its last version is `compose/bitwarden-broker/secrets_sync.py` at 42ae30f.

## Where secrets live today

| Location | Written by | Read by |
|---|---|---|
| `.env` (generated keys such as `POSTGRES_PASSWORD`, `N8N_ENCRYPTION_KEY`, `LLM_PROXY_ADMIN_TOKEN`) | `scripts/setup.sh` | compose interpolation and `env_file` |
| `<data root>/addons/<short id>/secrets/` (files at 0600, directory at 0700) | `scripts/addon.sh configure` and `connect`, through `scripts/lib/addon_store.py` | the add-on's services, through `services.<name>.mounts` |
| `compose/postgres/services/<slug>.env` | `make db-service-add NAME=<slug>` | the shared Postgres provisioner and the integration using that schema |
| Files named by `*_FILE` variables (`TWENTY_ENCRYPTION_KEY_FILE`, `TWENTY_DB_PASSWORD_FILE`, `LLM_PROXY_FALLBACKS_FILE`) | the user or setup | the service that variable belongs to |
| Files named by `LLM_BINDING_API_KEY_FILE`, `LIGHTRAG_LLM_BINDING_API_KEY_FILE` and `EMBEDDING_BINDING_API_KEY_FILE`, in place of the matching `*_API_KEY` | the user | `llm-proxy`, at start and again on SIGHUP or `POST /admin/reload` |
| `<data root>/web-browser/secrets/` (`agent`, `credentials` and `viewer` client tokens; `viewer` is also the noVNC viewer password; files at 0600, directory at 0700) | `scripts/web-browser-secrets.sh`, run by `setup.sh` and `make ensure-local`; an existing non-empty file is kept | `web-browser` (all three), the Hermes bootstrap containers (`agent`), `bitwarden-broker` (`credentials`) |
| `broker-api/api-token` under `BITWARDEN_HOST_DATA_DIR` (default `./data/addons/bitwarden`) | `scripts/vault.sh api-token`, run by `setup.sh` and `make ensure-local` whether or not the Bitwarden add-on is on | `bitwarden-broker` and `hermes`, which mount the `broker-api/` folder read-only |

## Rules a secret source must follow

These come from the removed sync and stay as requirements for whatever replaces it:

- Consumers declare the secrets they need; the source doesn't know where they land. No mapping
  table from item names to file paths.
- Write only when a value changed, through a temp file in the same folder, at mode 0600.
- Never create a missing file, and skip symbolic links.
- Refuse a name that maps to two files.
- Log names, never values.
- Refuse a value that compose's `env_file`, a shell and Python would read back differently:
  `$`, a backtick, `\`, quotes, `#`, leading or trailing whitespace, or a control character.
- Reload a consumer only when a file it reads changed.
