# Models

The model gateway sits between every stack service that calls a language or embedding model
and the model servers or providers that answer. The seam is HTTP: an OpenAI-compatible API on
the compose network. Consumers send a placeholder key, and only the gateway holds the real
upstream keys.

## Interface

A replacement answers at `http://llm-proxy:4000` on the compose network. Consumers have that
host and port written into their config (see [Consumers](#consumers)), so a gateway under
another name means re-pointing each of them.

| Path | Route | Forwards to |
|---|---|---|
| `/v1/...` | `chat` | the chat model (`LLM_BINDING_HOST`) |
| `/lightrag/v1/...` | `lightrag` | LightRAG's extraction model (`LIGHTRAG_LLM_BINDING_HOST`); the `chat` route when unset |
| `/embed/v1/...` | `embed` | the embedding model (`EMBEDDING_BINDING_HOST`) |
| `GET /health` | | 200 whenever the process is up, with or without keys |

Below each prefix the gateway serves its upstream's OpenAI API (`/chat/completions`,
`/embeddings` and so on); `llm-proxy` forwards any method and path there. Streamed responses
(`"stream": true`, server-sent events) must reach the client chunk by chunk as the upstream
sends them. Requests carry `Authorization: Bearer local-llm`. `llm-proxy` does not check the
placeholder: it drops it and sends the route's real key, or no `Authorization` header when the
key is empty.

The gateway reads its upstreams from the root `.env`:

| Variable | Meaning |
|---|---|
| `LLM_BINDING_HOST`, `LLM_BINDING_API_KEY` | `chat` upstream base URL and key |
| `LIGHTRAG_LLM_BINDING_HOST`, `LIGHTRAG_LLM_BINDING_API_KEY` | `lightrag` upstream; empty host means the `chat` route |
| `EMBEDDING_BINDING_HOST`, `EMBEDDING_BINDING_API_KEY` | `embed` upstream |
| `*_API_KEY_FILE` (one per route) | container path of a file holding the key; wins over the matching `*_API_KEY` |
| `LLM_PROXY_ADMIN_TOKEN` | bearer token for the admin API; compose refuses to start without it |
| `LLM_PROXY_ADMIN_PORT` | host loopback port for the admin API (default 4001) |

An empty key means the upstream needs none. Upstream URLs are OpenAI base URLs ending in
`/v1`; `compose/llm-proxy/proxy.py` appends `/v1` to one that lacks it.

`LLM_MODEL` and `EMBEDDING_MODEL` are model ids the consumers send, not gateway settings. The
gateway passes the client's `model` through unless a fallback names its own.

### Admin API

Downstream apps such as Boundary push keys and upstreams at runtime through an admin listener
published on `127.0.0.1:${LLM_PROXY_ADMIN_PORT}` only. Every request needs
`Authorization: Bearer $LLM_PROXY_ADMIN_TOKEN`.

| Request | Effect |
|---|---|
| `GET /admin/upstreams` | the route table without keys, plus `proxy.boot_id` and `proxy.started_at` |
| `POST /admin/upstreams` | replace the fields sent for `chat`, `lightrag` or `embed` (`url`, `api_key`, `fallbacks`), in memory only |
| `POST /admin/reload` | re-read key files, the fallbacks file and the model defaults file |
| `GET /admin/model-defaults` | the loaded per-model request defaults |

[extending.md](../extending.md#admin-api) gives the request and response bodies and states the
compatibility rule: paths, route names and fields stay, new fields may be added, and removing
or changing one gets a changelog entry first. `boot_id` changes on every process start, which
is how an admin client knows to push again. A replacement has to keep all of this, or Boundary
loses its way to set keys.

## Current implementation

`llm-proxy`, built from `compose/llm-proxy/` (`proxy.py`, Python standard library only, run as
`nobody` on `python:3.12-alpine`). It is in the `core` profile. Inference listens on 4000 and
is not published; the admin listener on 4001 is published on loopback. Nothing it holds is
written to disk: an admin push lasts until the process restarts, and a restart reads `.env`
and the key files again.

These behaviours belong to `llm-proxy` and are not part of the interface:

- **Fallback upstreams.** `chat` and `lightrag` can each carry an ordered list of
  `{url, api_key, model}` entries, from the admin API or the JSON file at
  `LLM_PROXY_FALLBACKS_FILE`. The proxy moves to the next entry when an upstream refuses the
  connection, cannot be reached within `LLM_CONNECT_TIMEOUT` (default 10s), or answers 429,
  500, 502, 503 or 504. Nothing is retried once a response has started. `embed` has no
  fallbacks, because another model's vectors would land in the same LightRAG index. See
  [Fallback upstreams](../extending.md#fallback-upstreams).
- **Per-model request defaults.** `LLM_PROXY_MODEL_DEFAULTS_FILE` (compose default
  `/etc/llm-proxy/model-defaults/local.json`, from the `model-defaults/` directory mounted
  read-only) maps model ids to request fields that the proxy adds to `/chat/completions`,
  `/completions` and `/responses` requests on `chat` and `lightrag` when the client leaves
  them unset. See [Model defaults](../model-defaults.md).
- **Default output cap.** `LLM_DEFAULT_MAX_TOKENS` (default 16384) adds `max_tokens`
  (`max_output_tokens` on `/responses`) to a generation request that sets none of
  `max_tokens`, `max_completion_tokens` or `max_output_tokens`. A model defaults entry's cap
  wins over it. `0` turns it off.
- **Timeouts.** `LLM_TIMEOUT` (default 600s) bounds the wait for response headers, which
  covers prompt prefill. `LLM_STREAM_IDLE_TIMEOUT` (default `LLM_TIMEOUT`) bounds a silent gap
  in a streamed body, and `LLM_STREAM_MAX_SECONDS` (default 3600, `0` off) bounds one request
  from start to finish. Past either, the proxy drops the client connection so the client sees
  a dead stream. A client that disconnects makes the proxy close the upstream request.
- **Keyless start.** `LLM_PROXY_AWAIT_PUSH` lists routes that answer 503 until an admin POST
  names them. See [Start without a key](../extending.md#start-without-a-key).
- **Key rotation.** SIGHUP (`docker compose kill -s HUP llm-proxy`) does the same as
  `POST /admin/reload`.
- **Served-by header.** Each proxied response carries `X-LLM-Proxy-Upstream: <route>;<index>`
  (0 is the primary), unless the upstream sent that header itself.

## Consumers

| Service | Base URL | Config source |
|---|---|---|
| Hermes, every profile | `http://llm-proxy:4000/v1`, `model.provider=custom`, `model.api_key=local-llm` | `compose/hermes/bootstrap-api-profile.sh` |
| LightRAG | `/lightrag/v1` for the LLM, `/embed/v1` for embeddings | `docker-compose.yml` (`LIGHTRAG_LLM_CLIENT_HOST`, `EMBEDDING_CLIENT_HOST` override them) |
| GPT Researcher, `gptr-mcp` | `OPENAI_BASE_URL=http://llm-proxy:4000/v1` for both their LLM and `EMBEDDING` settings | the `x-gptr` block in `docker-compose.yml` |
| OpenCode | every provider's `baseURL` and `apiKey` | `scripts/setup.sh` writes them into `opencode/opencode.local.json` |

Hermes, LightRAG, GPT Researcher, `gptr-mcp` and OpenCode wait for the gateway's healthcheck
before they start. With native Ollama (no `/v1`), setup points `LIGHTRAG_LLM_CLIENT_HOST` and
`EMBEDDING_CLIENT_HOST` at Ollama and LightRAG bypasses the gateway.

Hermes bootstrap also reads `LLM_BINDING_HOST` to size `agent.local_stream_stale_timeout`: 300s
for a local server, 180s for a cloud API. It cannot see an upstream pushed through the admin
API. [Configuring Hermes yourself](../extending.md#configuring-hermes-yourself) lists the
profile settings that depend on the gateway.
