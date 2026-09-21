[← Back to README](../README.md)

# Independent agents

Channel-agnostic Hermes **profiles** you create for distinct roles (engineer,
designer, researcher, …). Each agent has private memory and chat history, shares
project and global mounts with siblings, and can optionally attach a messaging
transport (today: [Buzz](buzz.md)) for shared rooms and DMs.

Runtime unit = Hermes profile under `data/hermes/profiles/<name>/` (gitignored).
This stack’s noun is **agent**; Buzz’s “employee” wording is only historical.

## Quick start

```bash
# Hermes must be running
docker compose up -d hermes

# Core agent (project + global memory; no messaging required)
make agent-create NAME=software-engineer DISPLAY_NAME="Software Engineer" \
  ROLE="Implement features; defer visual design to siblings."

make agent-create NAME=ui-ux-designer DISPLAY_NAME="UI/UX Designer" \
  ROLE="Own UX and visual design; coordinate via project files and shared rooms."

# Recreate so s6 starts each profile gateway
docker compose up -d --force-recreate hermes
```

Optional rooms/DMs when Buzz is set up:

```bash
make agent-create NAME=software-engineer WITH=buzz
# or attach later:
./scripts/agent-attach-buzz.sh software-engineer
```

Compatibility wrapper (always attaches Buzz):

```bash
make hermes-buzz-employee PROFILE=software-engineer DISPLAY_NAME="Software Engineer"
```

## What create does

| Step | Effect |
|---|---|
| `hermes profile create` | Isolated home: config, `SOUL.md`, memories, `state.db` |
| Seed `SOUL.md` | Role + three memory tiers + sibling markers ([template](../compose/hermes/agent-soul.template.md)) |
| Private `memories/` | **Not** shared with browser/api-server overlays |
| `gateway_state=running` | s6 starts this profile on hermes boot |
| Registry upsert | `data/hermes/agents/registry.json` |
| Sibling sync | Rewrites `<!-- agents:siblings -->` blocks in every agent `SOUL.md` |
| Buzz attach | Only if `WITH=buzz` or `HERMES_BUZZ_ENABLED=1` |

Edit `data/hermes/profiles/<name>/SOUL.md` after create to refine the role.
Content **outside** the sibling markers is yours; sync only rewrites the marker
block.

## Three memory tiers

| Tier | Where | Who sees it |
|---|---|---|
| **Private** | Profile `memories/` (`USER.md` / `MEMORY.md`) + that profile’s `session_search` | This agent only |
| **Project / room** | `/opt/projects/<slug>/` + shared room transcript (when a transport is attached) | All agents + human |
| **Global** | `/opt/memory/` | All agents + human |

The `working-memory` skill documents the same routing. Do not copy DM or session
content into project/global stores unless the human asks to share it. Agents
never read another profile’s `state.db` or `memories/`.

See [Memory](memory.md) and [Projects](projects.md).

## Registry

```text
data/hermes/agents/registry.json
```

Per agent: `name`, `display_name`, `role`, `created_at`, optional
`transports.buzz.pubkey`. UI builders and tooling can list agents from this file
without knowing Buzz. Soft-checked by `make doctor` and `sync-buzz-profile.sh`.

## Messaging transports

Buzz is the current expression of shared rooms and DMs — not part of core agent
identity. Details: [Buzz](buzz.md).

| Without Buzz | With Buzz |
|---|---|
| Collaborate via `/opt/projects` and `/opt/memory` | Plus channels (`@mention`) and private DMs |
| No room transcript | Room history is the shared chat memory |

## Related

- [Hermes Agent](hermes.md) — profiles, mounts, SOUL
- [Buzz](buzz.md) — optional transport attach
- [Memory](memory.md) — curated global notes vs session search
- [Projects](projects.md) — shared project briefs
