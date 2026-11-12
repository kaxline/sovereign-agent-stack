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
