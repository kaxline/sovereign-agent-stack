#!/usr/bin/env python3
"""Check which sampling fields a model server applies, before shipping an example.

Servers accept OpenAI-style requests but may ignore fields they do not support,
with no error. This sends short fixed prompts with and without each field and
compares the output, using greedy decoding (top_k 1, temperature 0) where the
comparison needs a fixed baseline:

    ./scripts/model-defaults-probe.py qwen/qwen3.6-35b-a3b
    ./scripts/model-defaults-probe.py MODEL --url http://127.0.0.1:1234/v1

"applied" means the field changed the output as expected. "no effect" means it
did not, so the server most likely ignores it. top_k is the reference: if it
shows no effect, the other results say nothing. See docs/model-defaults.md.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

OCEAN = "Write one short sentence about the ocean."
NUMBERS = "List the numbers one to forty in words, separated by commas."


def make_ask(url: str, model: str, key: str):
    def ask(prompt: str, max_tokens: int, **fields) -> str:
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, **fields}
        req = urllib.request.Request(
            url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            message = json.load(resp)["choices"][0]["message"]
        # Thinking models may spend the whole budget on reasoning; compare both.
        return (message.get("reasoning_content") or message.get("reasoning") or "") + (message.get("content") or "")

    return ask


def report(name: str, applied: bool, detail: str) -> None:
    print("  %-18s %-10s %s" % (name, "applied" if applied else "no effect", detail))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("model", help="model id exactly as the server lists it")
    parser.add_argument("--url", default="http://127.0.0.1:1234/v1", help="OpenAI-compatible base URL")
    parser.add_argument("--key", default="local-llm", help="bearer key, if the server needs one")
    args = parser.parse_args()
    ask = make_ask(args.url, args.model, args.key)
    print("Probing %s at %s" % (args.model, args.url))

    # temperature and top_k: at temperature 2 sampling is noisy; top_k 1 makes it repeatable.
    hot = [ask(OCEAN, 40, temperature=2.0, top_p=1.0, top_k=0) for _ in range(3)]
    pinned = [ask(OCEAN, 40, temperature=2.0, top_p=1.0, top_k=1) for _ in range(3)]
    top_k = len(set(pinned)) == 1
    report("top_k", top_k, "3 runs at temperature 2 with top_k 1 were %s" % ("identical" if top_k else "different"))
    report("temperature", len(set(hot)) > 1, "3 runs at temperature 2 without top_k were %s"
           % ("different" if len(set(hot)) > 1 else "identical"))
    narrow = [ask(OCEAN, 40, temperature=2.0, top_p=0.01, top_k=0) for _ in range(3)]
    report("top_p", len(set(narrow)) == 1, "3 runs at temperature 2 with top_p 0.01 were %s"
           % ("identical" if len(set(narrow)) == 1 else "different"))

    # Penalties: greedy output with and without the penalty.
    greedy = dict(temperature=0.0, top_k=1)
    base = ask(NUMBERS, 400, **greedy)
    if ask(NUMBERS, 400, **greedy) != base:
        print("  greedy output is not repeatable on this server; penalty results below are not reliable")
    for name, value in (("presence_penalty", 2.0), ("frequency_penalty", 2.0), ("repeat_penalty", 1.5),
                        ("repetition_penalty", 1.5)):
        changed = ask(NUMBERS, 400, **greedy, **{name: value}) != base
        report(name, changed, "greedy output with %s %s was %s" % (name, value, "different" if changed else "identical"))
    print("min_p is not probed: an example that sets it to 0 turns it off.")
    if not top_k:
        sys.exit("top_k showed no effect, so these results are not conclusive")


if __name__ == "__main__":
    main()
