# Model defaults

`llm-proxy` can add request fields per model, such as the sampling settings a
model's authors recommend. Hermes, as this stack configures it, sends no
sampling settings, so a local model runs on whatever its server defaults to.
The proxy keys the settings on the `model` a
request names. When you switch models, the old model's settings stop applying,
and switching back brings them back.

The settings live in `model-defaults/local.json`, which is gitignored and
yours to edit. Tested settings for some models ship in
`model-defaults/examples/`.

## Install a tested example

```bash
make model-defaults
```

```bash
make model-defaults-add EXAMPLE=qwen3.6-35b-a3b
```

The first lists the examples and the models `local.json` holds. The second
merges the example's models into `local.json`, creating it if needed, and
reloads `llm-proxy` if it is running. A model `local.json` already lists is
kept as you have it; add `REPLACE=1` to overwrite it with the example's
entry. `./scripts/model-defaults.py` does the same without make.

Copying an example over `local.json` also works when you use only that model:

```bash
cp model-defaults/examples/qwen3.6-35b-a3b.json model-defaults/local.json
```

Read the example's section under [Tested examples](#tested-examples) first.
It says which fields the server we tested on applies.

## The file

`local.json` maps model ids to the fields to add:

```json
{
  "_about": "notes for yourself",
  "qwen/qwen3.6-35b-a3b": {
    "_source": "where the values come from",
    "temperature": 1.0,
    "top_k": 20,
    "presence_penalty": 1.5
  }
}
```

- A key is the model id exactly as clients send it: `LLM_MODEL` in `.env`, or
  the `id` your model server lists at `/v1/models`. Case does not matter. A
  model served under several ids needs an entry for each.
- Keys that start with `_`, at the top level or inside an entry, are notes and
  are ignored.
- Each field is added to the top level of the request only when the request
  leaves it unset or sets it to `null`. A value the client sends wins. A nested
  object the client sends, such as `chat_template_kwargs`, is not merged with
  one from the file.
- An entry may set an output cap (`max_tokens`, `max_completion_tokens` or
  `max_output_tokens`). It is skipped when the request sets any of the three,
  and it takes precedence over `LLM_DEFAULT_MAX_TOKENS`, which is applied after
  the file. Hermes sets `max_tokens` itself, so a cap here does not reach it.
- An entry may not set `model`, `messages`, `prompt` or `input`.

The proxy applies the file to `/chat/completions`, `/completions` and
`/responses` requests on the chat and LightRAG routes. Embedding requests are
untouched. With [fallback upstreams](extending.md#fallback-upstreams), each
attempt gets the fields for the model it is sent with, so a fallback that names
its own `model` gets that model's entry, not the primary's. A fallback without
a `model` gets the client's model and its entry, so check that the fallback's
API accepts those fields.

### Reload and check

The proxy reads the file at start. After editing it, reload:

```bash
make model-defaults-reload
```

That sends SIGHUP to `llm-proxy`. `POST /admin/reload` on the admin port does
the same. A file that is not valid JSON, or that breaks the rules above, is
logged and the loaded settings stay in place; at start that means none. A
missing file means no settings. The log line names the file and the problem:

```bash
docker compose logs llm-proxy | grep 'model defaults'
```

To see what the proxy has loaded:

```bash
curl -s -H "Authorization: Bearer $LLM_PROXY_ADMIN_TOKEN" http://127.0.0.1:4001/admin/model-defaults
```

### Location

Compose mounts the `model-defaults/` directory read-only at
`/etc/llm-proxy/model-defaults` and sets `LLM_PROXY_MODEL_DEFAULTS_FILE` to
`local.json` in it. It mounts the directory rather than the file because a
file mount keeps pointing at the old file after an editor or the script
replaces it.

Set `LLM_PROXY_MODEL_DEFAULTS_FILE` in `.env` to another container path to
read a different file, or to an empty value to turn the feature off. An app
that runs this stack can mount its own file the same way.

## Tested examples

An example lists settings from the model's authors, and records the server we
tested it on and what that server did with each field. Servers accept fields
they do not support without an error, so a field in an example may have no
effect on your server. Check yours with the probe:

```bash
./scripts/model-defaults-probe.py qwen/qwen3.6-35b-a3b
```

It sends short fixed prompts to the model server (default
`http://127.0.0.1:1234/v1`, `--url` to change) and reports, per field, whether
the output changed: top_k, temperature and top_p by comparing repeated runs at
temperature 2, and the penalties by comparing greedy output with and without
each one. It takes a few minutes on a local 35B model.

### qwen3.6-35b-a3b

`model-defaults/examples/qwen3.6-35b-a3b.json` holds the
[model card's](https://huggingface.co/Qwen/Qwen3.6-35B-A3B) recommendation for
thinking mode on general tasks: `temperature` 1.0, `top_p` 0.95, `top_k` 20,
`min_p` 0 and `presence_penalty` 1.5. The card suggests `presence_penalty`
between 0 and 2 to reduce endless repetition, and warns that higher values
can cause language mixing. For precise coding tasks it recommends
`temperature` 0.6 and `presence_penalty` 0 instead.

Tested on 2026-11-05 with LM Studio 0.4.26, `qwen/qwen3.6-35b-a3b`, 4-bit MLX:

| Field | Result |
|---|---|
| `temperature`, `top_k` | Applied |
| `top_p` | No effect in the probe |
| `presence_penalty`, `frequency_penalty` | No effect |
| `repeat_penalty` | Applied (not in the example; the card recommends no repetition penalty) |
| `min_p` | Not probed; 0 turns it off |

The proxy adds the example's fields to requests, which we confirmed through a
running proxy. On this LM Studio build, `presence_penalty` is ignored, so the
example does not guard against repetition loops there. It may on a server
that applies it.

## Add an example

An example earns its place when someone has tested it, so keep to this:

1. Take the values from the model's authors (model card, `generation_config.json`)
   and put the link and the date you read it in `_source`.
2. Run `./scripts/model-defaults-probe.py` against the server and build you
   use, install the example with `make model-defaults-add`, and send a request
   through the stack to confirm the model still answers.
3. Name the file after the model, put the server, build, date and per-field
   results in `_tested`, and add a section above with the same results.
