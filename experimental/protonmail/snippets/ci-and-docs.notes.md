## CI

Include `core,mail` (and combined matrix entries with `mail`) in compose-config profile loops.

## Docs index (`docs/README.md`)

| [Proton Mail Bridge](protonmail-bridge.md) | `mail` | IMAP/SMTP for n8n |

## README profiles table

| **`mail`** | Proton Mail Bridge for n8n IMAP/SMTP |

## `.gitignore`

```
compose/protonmail-bridge/credentials.env
```

## SECURITY.md

Keep the Proton Mail Bridge section (unofficial image, compose-internal only, treat credentials.env as secret).

## n8n credentials table

| Proton Mail (Bridge) | IMAP | `compose/protonmail-bridge/credentials.env` (after `./scripts/proton-bridge-init.sh`) |
