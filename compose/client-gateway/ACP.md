# ACP pin

The gateway serves ACP v1. What it is built and tested against:

| What | Version | Checked |
|---|---|---|
| ACP schema (`schema/acp-v1.json`, `schema/acp-v1-meta.json`) | v1.31.0 (2026-11-10), stable | 2026-11-12 |
| Transport | Streamable HTTP & WebSocket RFD, revision 2026-10-30, WebSocket profile only | 2026-11-12 |
| TypeScript SDK (Boundary) | `@agentclientprotocol/sdk` 2.6.0, `experimental/ws-client` with the `ws` package | 2026-11-12 |
| Python SDK | `agent-client-protocol` 1.0.0 (schema v1.23.0); not used by the gateway | 2026-11-12 |
| Hermes | `nousresearch/hermes-agent:v0.23.1`, legacy protocol on the dashboard's `/api/ws` | 2026-11-12 |

The schema files are copied unchanged from the ACP repository's release assets. To move the pin,
replace both files, update this table, and run `scripts/test-client-gateway-*.py`.

Transport choices that follow the RFD: the server sends `Acp-Connection-Id` on the 101 response;
one JSON-RPC message per text frame; sessions are routed by `sessionId` in the body, with no
`Acp-Session-Id` header on WebSocket.

Drafts implemented early under `_io.github.kaxline.stack/` names are listed in `schema/stack.json`
as they land, each with the RFD it mirrors.
