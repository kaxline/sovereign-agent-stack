[← Back to README](../README.md)

# AI text detector

The `detector` add-on scores how likely a passage is to be AI-written, using
Pangram or GPTZero under the user's own account. Every check sends the text to
that company and costs money on the account, so nothing calls it on its own:
Boundary calls it when the user presses a button, and the agent gets it only
when the user turns on the add-on's agent setting.

| Service | Role | Profile | Networks |
|---|---|---|---|
| `detector` | Holds the keys and the bearer token, calls the provider | `detector` (`DETECTOR_ENABLED`) | `detector` (internal), `detector-egress` |
| `detector-mcp` | `check_text` for Hermes, forwards to `detector:8000`; no key | `detector-agent` (`DETECTOR_AGENT_ENABLED`) | `detector` (internal), `detector-agent` (internal, shared with `hermes`) |

Hermes is not on the `detector` network, because `detector:8000` takes
requests without a token. `hermes-worker`, the agent's shell, is on neither.

## Turning it on

```bash
echo '{"provider": "pangram", "pangram_key": "<key>"}' | ./scripts/addon.sh configure detector
./scripts/addon.sh enable detector
```

`provider` is `pangram` (the default) or `gptzero`. Each provider has its own
optional key setting, `pangram_key` and `gptzero_key`, stored as files under
`data/addons/detector/secrets/`. The add-on enables without a key; a check
then returns `no_key`. Changing the provider or a key takes effect on the next
check, with no restart.

`enable` runs `scripts/detector.sh init`, which writes the bearer token to
`data/addons/detector/secrets/http_token` (mode 0600) if there is none.
`scripts/detector.sh rotate` replaces it; the service reads the file on every
request.

To give the agent the tool, set the agent setting:

```bash
echo '{"agent": true}' | ./scripts/addon.sh configure detector
./scripts/addon.sh enable detector
```

That sets `DETECTOR_AGENT_ENABLED=1`, starts `detector-mcp`, and both Hermes
bootstraps register `mcp_servers.detector` with `check_text` on the default
(dashboard, CLI, Boundary chats) and browser (WebUI) profiles. It is never
registered on `api-server`, and `permissions.cron: false` fences it out of
cron jobs. On a profile that also reads mail, the tool lets the agent send any
text it holds to the provider, so it stays off unless the user wants it.

## Calling it from the host

`detector` publishes `127.0.0.1:${DETECTOR_PORT:-8790}`, mapped to its
container port 8001. Nothing listens on another interface.

```
POST /check
Authorization: Bearer <contents of data/addons/detector/secrets/http_token>
Content-Type: application/json

{"text": "..."}
```

The `Host` header must be `127.0.0.1`, `localhost` or `[::1]` (any port),
which stops a web page from reaching the port through DNS rebinding; the
bearer header stops a page from posting to it at all. `GET /health` takes the
same token and returns `{"ok": true, "provider": "<name>"}`.

`scripts/detector.sh check < file.txt` sends one paid check through the port
and prints the answer.

### Result

```json
{
  "provider": "pangram",
  "model": "4.0",
  "score": 0.75,
  "label": "Mixed",
  "segments": [{"start": 0, "end": 212, "score": 0.97}],
  "checkedAt": "2026-11-11T15:40:00Z"
}
```

- `score` is 0 to 1 on the provider's own scale, and scores from different
  providers aren't comparable. For Pangram it is `fraction_ai +
  fraction_ai_assisted`, the share of the text Pangram classed as AI-generated
  or AI-assisted. For GPTZero it is `completely_generated_prob`.
- `label` is the provider's own: Pangram's `prediction_short` (`AI`, `Human`,
  `Mixed`), GPTZero's `predicted_class`.
- `model` is the provider's model version, when it sends one.
- `segments[].start` and `end` are UTF-16 code unit offsets into the submitted
  text (JavaScript string indices), end exclusive. Pangram may normalize the
  text before scoring and GPTZero returns sentences without offsets, so the
  service finds each piece in the submitted text, first exactly and then with
  any run of whitespace matching any other. A piece it can't find is left
  out, so segments may not cover the whole text.

### Errors

Every error is `{"error": {"code", "message"}}` with an HTTP status.

| Code | Status | When |
|---|---|---|
| `too_short` | 422 | Fewer than 50 words (split on whitespace). Checked before any provider call. |
| `too_long` | 413 | Over 50,000 characters (code points), or the provider refused the size. Checked before any provider call. |
| `no_key` | 409 | No key is set for the chosen provider. |
| `quota` | 429 | The provider answered 402 (out of credits) or 429 (rate limit). `retryAfter` (seconds) and a `Retry-After` header when the provider sent one. |
| `provider_error` | 502 | The provider refused the key (401/403), failed, answered something unreadable, or couldn't be reached. |
| `timeout` | 504 | No finished answer within 60 seconds (`DETECTOR_TIMEOUT`). |
| `bad_input` | 400 | The body isn't JSON `{"text": <string>}`. |
| `unauthorized` | 401 | Missing or wrong bearer token, or no token file. |
| `forbidden` | 403 | A `Host` other than the loopback names above. |

The service logs the method, path, status, error code and duration of each
request. It never logs the text, the token or the provider's answer.

## Providers

Pangram: `POST /task` on `https://text.external-api.pangram.com` with model
`pangram-4` (`DETECTOR_PANGRAM_MODEL`), then `GET /task/{id}` once a second
until the stage is `STAGE_SUCCESS` or `STAGE_FAILED`, from
[Pangram's API reference](https://docs.pangram.com/api-reference/ai-detection).

GPTZero: `POST /v2/predict/text` on `https://api.gptzero.me` with `{"document": text}`
and the key in `x-api-key`. GPTZero's own reference couldn't be read when the
adapter was written, so its field names (`documents[0].completely_generated_prob`,
`predicted_class`, `sentences[].sentence`, `generated_prob`) come from
third-party descriptions. Check them against a real response before relying
on GPTZero.

Neither adapter has been run against the real API. `scripts/test-detector.py`
runs both against a stub.

## Later: a relay

Boundary CR 0030 R3 keeps room for a shared business account whose key lives
in a relay service. When one exists, it becomes another `provider` value with
a URL and a per-install token, returning the same result shape. Nothing is
built for it.
