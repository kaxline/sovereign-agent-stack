# Cron fence

The bootstraps' cron fence (`fence_mail_from_cron` in
`compose/hermes/bootstrap-api-profile.sh`) keeps mail, messages, the browser
and add-ons without `permissions.cron: true` away from Hermes cron jobs that
don't ask for them. The rules are in
[contracts/agent.md](contracts/agent.md#scheduler); the tests are
`scripts/test-addon-cron-fence.sh`.

## Why cron needs a fence

A cron job runs with nobody watching. If it has no `enabled_toolsets` of its own,
or has a list that names no MCP server, Hermes gives it every enabled MCP server
on its profile, alongside web search, the browser and a shell. Text the job reads
from mail or messages could then steer it into sending that data somewhere else.

```mermaid
flowchart LR
    job["Cron job on a profile<br/>no enabled_toolsets"] --> tools
    subgraph tools["Tools Hermes gives it without a fence"]
        mail["mail / drafts / messages<br/>untrusted text in"]
        addons["add-ons without<br/>permissions.cron: true"]
        browser["browser<br/>signed-in sessions"]
        out["web, terminal,<br/>other MCP servers"]
    end
    mail -- "a message's text steers the job" --> out
    mail -- "or" --> browser
    out --> leak(["private data leaves the machine"])
    browser --> leak
```

## Which list a cron job's tools come from

```mermaid
flowchart TD
    start["Cron job runs"] --> own{"Has its own<br/>enabled_toolsets?"}
    own -- yes --> names{"Names at least one<br/>MCP server or no_mcp?"}
    names -- yes --> exact["Exactly that list<br/>a fenced tool only if named"]
    names -- no --> allmcp["That list plus every enabled MCP server<br/>gap: the fence does not cover it,<br/>make doctor flags mail jobs"]
    own -- no --> platform{"platform_toolsets.cron<br/>set on the profile?"}
    platform -- yes --> fenced["The fence's list:<br/>cron toolsets without browser<br/>plus unfenced MCP servers"]
    platform -- no --> everything["hermes-cron bundle plus every MCP server<br/>browser and mail included"]
```

## Agent profiles

Until the change below, the fence ran on the default profile and on each
bootstrap's own profile. Profiles made with `make agent-create` had no
`platform_toolsets.cron`, so their jobs fell into the "everything" box above.

```mermaid
flowchart LR
    subgraph before["Before"]
        b1["default"]:::ok
        b2["api-server"]:::ok
        b3["browser"]:::ok
        b4["agent profile A"]:::bad
        b5["agent profile B"]:::bad
    end
    subgraph after["Now"]
        a1["default"]:::ok
        a2["api-server"]:::ok
        a3["browser"]:::ok
        a4["agent profile A"]:::ok
        a5["agent profile B"]:::ok
    end
    before --> after
    classDef ok fill:#d4edda,stroke:#2e7d32,color:#1b3d1f
    classDef bad fill:#f8d7da,stroke:#c62828,color:#4a1212
```

Green: fenced. Red: cron jobs keep mail, messages, fenced add-ons and the browser.

## When the fence runs

`fence_mail_from_cron_all_profiles` uses the same loop over profiles as
`apply_model_proxy_all_profiles`. hermes-api-bootstrap runs the shared steps
(`HERMES_BOOTSTRAP_SHARED=1`). hermes-browser-bootstrap runs after it with
the shared steps off and does only its own profile, after registering its own
MCP servers. A bootstrap whose stamp matches the last run skips the fence entirely.

```mermaid
sequenceDiagram
    participant API as hermes-api-bootstrap<br/>(shared = 1)
    participant BR as hermes-browser-bootstrap<br/>(shared = 0)
    participant H as data/hermes
    participant HM as hermes
    API->>H: fence default config.yaml
    API->>H: fence profiles/api-server
    loop every other dir under profiles/
        API->>H: fence profiles/name (skipped if no config.yaml)
    end
    API-->>BR: completed successfully
    BR->>H: register browser MCP servers
    BR->>H: fence profiles/browser again
    BR-->>HM: completed successfully
    HM->>HM: gateways start, cron reads platform_toolsets.cron
```

Each fenced profile costs about 1.6 s (one Python process that loads Hermes'
toolset code).

## What the fence does to one profile

`fence_mail_from_cron <profile>` reads that profile's `config.yaml`.

```mermaid
flowchart TD
    s["fence_mail_from_cron profile"] --> cfg{"config.yaml exists?"}
    cfg -- no --> skip(["skip"])
    cfg -- yes --> find["Work out what is fenced:<br/>servers at mail-mcp, mail-drafts-mcp, messages-mcp<br/>add-on servers by MCP name or URL scheme+host+port<br/>the browser toolset"]
    find --> plug["Vault plugin toolsets enabled here<br/>go into known_plugin_toolsets.cron"]
    plug --> mod{"Add-on manifest module<br/>loaded?"}
    mod -- "no, and the list is ours" --> keep(["keep the list written last time"])
    mod -- yes --> cur{"platform_toolsets.cron<br/>today?"}
    cur -- "unset, or matches<br/>.mail-cron-toolsets.json" --> pin["Pin: Hermes cron toolsets without browser<br/>+ every unfenced MCP server<br/>record it in .mail-cron-toolsets.json"]
    cur -- "written by hand" --> strip["Keep it, minus fenced servers and browser<br/>hermes-cron / hermes-cli expanded<br/>to their toolsets without browser"]
    strip --> nomcp{"Names any MCP server<br/>or no_mcp?"}
    nomcp -- no --> addothers["Append the unfenced MCP servers<br/>else Hermes would add all of them"]
    nomcp -- yes --> write(["write config.yaml"])
    addothers --> write
    pin --> write
```

A job that needs a fenced tool, like `make mail-triage-cron`, names it in its own
`enabled_toolsets` and gets it.
