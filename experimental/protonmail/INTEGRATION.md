# Experimental: Proton Mail Bridge (`mail` profile)

This branch preserves the Proton Mail Bridge work so it can be re-merged without rediscovery.

## Files on this branch

| Path | Role |
|---|---|
| `compose/protonmail-bridge/credentials.env.example` | Template only — never commit real `credentials.env` |
| `compose/protonmail-bridge/updates-block/` | Read-only mount that blocks Bridge auto-updates on arm64 |
| `docs/protonmail-bridge.md` | Operator guide |
| `scripts/proton-bridge-init.sh` | Interactive Proton login + 2FA |
| `scripts/sync-proton-bridge-credentials.sh` | Push Bridge IMAP secrets into n8n demo credentials |
| `experimental/protonmail/snippets/` | Exact snippets to splice into shared mainline files |

## Re-integrate onto main

1. Copy `compose/protonmail-bridge/` (example + updates-block), `docs/protonmail-bridge.md`, and both scripts into `scripts/`.
2. Insert the service from `snippets/docker-compose.protonmail.yml.snippet` and add `protonmail_bridge` under `volumes:`.
3. Merge `snippets/setup.mail.sh.snippet` into `scripts/setup.sh` (`--mail`, `setup_mail_profile`, footer).
4. Append `snippets/env.example.protonmail.snippet` and the `mail` profile line to `.env.example`.
5. Restore Proton IMAP generation in `scripts/generate-n8n-credentials.js` from `snippets/generate-n8n-credentials.proton.snippet.js`.
6. Follow `snippets/ci-and-docs.notes.md` for README, docs index, CI, `.gitignore`, SECURITY, and n8n credential docs.

Requires a **paid Proton plan**. Keep IMAP/SMTP compose-internal (no host ports). Quit the Mac Bridge app before starting the container.
