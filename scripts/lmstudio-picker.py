#!/usr/bin/env python3
"""List LM Studio models and pick one (auto or interactive)."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

EMBEDDING_HINTS = ("embed", "embedding", "nomic-embed", "bge-")


def is_embedding(model_id: str) -> bool:
    lid = model_id.lower()
    return any(hint in lid for hint in EMBEDDING_HINTS)


def tty_prompt(prompt: str) -> str:
    """Read a line from /dev/tty so input works inside shell command substitution."""
    with open("/dev/tty", "r", encoding="utf-8") as tty_in, open(
        "/dev/tty", "w", encoding="utf-8"
    ) as tty_out:
        tty_out.write(prompt)
        tty_out.flush()
        return tty_in.readline().strip()


def fetch_model_ids(url: str) -> list[str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = json.load(resp)
    except urllib.error.URLError as exc:
        print(f"Could not reach {url}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    ids = [m.get("id", "") for m in (payload.get("data") or []) if m.get("id")]
    if not ids:
        print(f"No models listed at {url} — load a model in LM Studio first", file=sys.stderr)
        raise SystemExit(1)
    return ids


def auto_pick(ids: list[str], current: str) -> str:
    chat_ids = [mid for mid in ids if not is_embedding(mid)]
    if chat_ids:
        if current in chat_ids:
            return current
        return chat_ids[0]
    print(
        "Warning: only embedding models are listed; pick a chat model in LM Studio "
        "or pass MODEL_ID explicitly.",
        file=sys.stderr,
    )
    return ids[0]


def interactive_pick(url: str, ids: list[str], current: str) -> str:
    chat_ids = [mid for mid in ids if not is_embedding(mid)]
    embedding_ids = [mid for mid in ids if is_embedding(mid)]

    print(f"Models served by LM Studio ({url}):\n", file=sys.stderr)
    default_idx = 0
    for i, mid in enumerate(ids, start=1):
        tags = []
        if is_embedding(mid):
            tags.append("embedding")
        if mid == current:
            tags.append("current")
            default_idx = i
        suffix = f"  [{', '.join(tags)}]" if tags else ""
        print(f"  {i}) {mid}{suffix}", file=sys.stderr)

    if embedding_ids and chat_ids:
        print(
            "\nEmbedding models are marked — they are poor chat choices.",
            file=sys.stderr,
        )

    prompt = f"\nChoose model [1-{len(ids)}"
    if default_idx:
        prompt += f", default {default_idx}"
    prompt += "]: "

    while True:
        try:
            raw = tty_prompt(prompt)
        except OSError as exc:
            print(f"Could not read from terminal: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc

        if raw == "" and default_idx:
            return ids[default_idx - 1]

        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(ids):
                chosen = ids[idx - 1]
                if is_embedding(chosen):
                    confirm = tty_prompt(
                        f"'{chosen}' looks like an embedding model. Use anyway? [y/N]: "
                    ).lower()
                    if confirm not in ("y", "yes"):
                        continue
                return chosen

        if raw in ids:
            return raw

        print("Invalid choice — enter a number or exact model id.", file=sys.stderr)


def main() -> None:
    if len(sys.argv) != 4:
        print("Usage: lmstudio-picker.py URL CURRENT MODE", file=sys.stderr)
        raise SystemExit(2)

    url, current, mode = sys.argv[1:4]
    ids = fetch_model_ids(url)

    if mode != "interactive":
        choice = auto_pick(ids, current)
        if len(ids) > 1:
            print(
                f"Auto-selected '{choice}' from {len(ids)} loaded models "
                f"(pass -y to skip this message; run from a terminal for a menu).",
                file=sys.stderr,
            )
        print(choice)
        return

    print(interactive_pick(url, ids, current))


if __name__ == "__main__":
    main()
