[← Back to README](../README.md)

# Proton Mail Bridge (opt-in)

Containerized Proton Mail Bridge giving n8n IMAP/SMTP access to a Proton account over the compose network.

Containerized [Proton Mail Bridge](https://proton.me/mail/bridge) gives n8n IMAP/SMTP access to a Proton account over the compose network. Requires a **paid Proton plan**. Uses the unofficial [shenxn/protonmail-bridge](https://github.com/shenxn/protonmail-bridge-docker) image with socat so other containers can reach Bridge (Bridge itself only listens on `127.0.0.1` inside the container).

**Quit the Mac Bridge app** before starting the container — only one Bridge instance can run at a time.

## Enable

```bash
./scripts/setup.sh --mail          # enables compose profile mail + defaults
./scripts/proton-bridge-init.sh    # one-time interactive Proton login + 2FA
docker compose up -d               # when COMPOSE_PROFILES=mail is in .env
```

Or per command: `docker compose --profile mail up -d`

Combine with other profiles: `COMPOSE_PROFILES=core,mail` in `.env`.

## n8n IMAP settings

The **Proton Mail (Bridge)** credential is auto-generated after init. If you create it manually:

| Field | Value |
|---|---|
| Host | `protonmail-bridge` |
| Port | `143` |
| SSL/TLS | off (Bridge uses STARTTLS) |
| Allow Self-Signed Certificates | on |
| User / Password | Bridge-generated (from init `info`, not your Proton login) |

SMTP (if sending mail): host `protonmail-bridge`, port `25`, same Bridge credentials.

## Re-sync credentials

If Bridge rotates its local IMAP password:

```bash
./scripts/sync-proton-bridge-credentials.sh

docker compose run --rm --entrypoint /bin/sh n8n-import -c \
  'n8n import:credentials --separate --input=/demo-data/credentials'
```

## Verification

```bash
docker compose --profile mail ps protonmail-bridge
docker exec -it n8n sh -c 'nc -zv protonmail-bridge 143'
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Another instance is running" during init | Quit Mac Bridge app; `docker compose --profile mail stop protonmail-bridge` |
| n8n connection refused | Ensure `COMPOSE_PROFILES=mail` and Bridge container is healthy |
| First sync slow | Bridge initial sync can take several minutes — watch `docker compose --profile mail logs -f protonmail-bridge` |
| Credential missing in n8n | Run init before first `docker compose up`, or re-import credentials (see above) |
| Healthy but IMAP fails / Rosetta / exec format error | Bridge auto-updated to an amd64 binary; this stack bind-mounts an empty read-only `updates/` dir to prevent that. Reset with `docker compose --profile mail down` then `docker volume rm assistant_protonmail_bridge` and re-run `./scripts/proton-bridge-init.sh` |
