"""make doctor's file checks, run through stack_python so the host needs no python3.

Standard library only. Each command prints what doctor reports and exits 0,
except where noted. Paths are relative to the current directory (the repo).

  yaml-get PATH DOTTED            the scalar at DOTTED; exit 1 when the key is absent
  yaml-list-missing PATH DOTTED ITEM...
                                  the ITEMs not in the list at DOTTED, one per line;
                                  ITEM may be a|b, met when either is listed
  agents                          names in data/hermes/agents/registry.json
  corpus-dim REGISTRY SLUG        the corpus's embedding_dim, or nothing
  nested-binds PROJECTS           (compose config JSON on stdin) hermes binds under
                                  /opt/projects/ whose host folder is missing
  terminal-backend WANT           profiles whose terminal.backend is not WANT, or
                                  NOCONFIG when there is no config.yaml at all
  override-mounts FILE SERVICE [--host-only]    (FILE - reads stdin)
                                  the volume entries of SERVICE in an override file
                                  (with --host-only, the host paths of absolute binds)
  proxy-mismatch WANT             model clients whose base URL is not WANT

The YAML readers handle the block style the bootstrap writes: nested mappings
by indentation, `- item` lists (indented or at the key's indent) and `[a, b]`
flow lists. They need no PyYAML, so a missing package cannot turn a check OK.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _lines(path: Path) -> list[tuple[int, str]]:
    """(indent, text) for each line that is not blank or a comment. A path of
    `-` reads stdin, for files the image cannot see (an override that is a
    symlink out of the repo)."""
    if str(path) == "-":
        raw = sys.stdin.read()
    else:
        raw = path.read_text(encoding="utf-8", errors="replace")
    out = []
    for line in raw.splitlines():
        text = line.lstrip(" ")
        if not text or text.startswith("#"):
            continue
        out.append((len(line) - len(text), text.rstrip()))
    return out


def _unquote(value: str) -> str:
    return value.strip().strip("'\"")


def _key_value(text: str, key: str) -> str | None:
    """The text after `key:` when TEXT is that key's line, else None."""
    for spelled in (key, f'"{key}"', f"'{key}'"):
        if text == spelled + ":":
            return ""
        if text.startswith(spelled + ":") and text[len(spelled) + 1] in " \t":
            return text[len(spelled) + 1 :].strip()
    return None


def _find(lines: list[tuple[int, str]], dotted: str):
    """(value, block) for DOTTED: the inline value and the lines nested under it.

    Returns None when a key on the path is missing.
    """
    start, end, parent = 0, len(lines), -1
    value = ""
    for part in dotted.split("."):
        child = None
        hit = None
        for i in range(start, end):
            indent, text = lines[i]
            if indent <= parent:
                end = i
                break
            if child is None:
                child = indent
            if indent != child:
                continue
            found = _key_value(text, part)
            if found is not None:
                hit = i
                value = found
                break
        if hit is None:
            return None
        key_indent = lines[hit][0]
        start, parent = hit + 1, key_indent
        end = len(lines)
        for j in range(start, len(lines)):
            indent, text = lines[j]
            # A list may sit at the key's own indent (`key:` then `- item`).
            if indent < key_indent or (indent == key_indent and not text.startswith("- ")):
                end = j
                break
        # Children at the key's indent are list items; let the next part see them.
        parent = key_indent - 1
    return value, lines[start:end]


def yaml_get(path: Path, dotted: str) -> str | None:
    found = _find(_lines(path), dotted)
    return None if found is None else _unquote(found[0])


def yaml_list(path: Path, dotted: str) -> list[str]:
    found = _find(_lines(path), dotted)
    if found is None:
        return []
    value, block = found
    if value.startswith("[") and value.endswith("]"):
        return [_unquote(v) for v in value[1:-1].split(",") if v.strip()]
    if not block:
        return []
    top = block[0][0]
    return [_unquote(text[2:]) for indent, text in block if indent == top and text.startswith("- ")]


def _configs() -> list[Path]:
    paths = [Path("data/hermes/config.yaml")]
    profiles = Path("data/hermes/profiles")
    if profiles.is_dir():
        paths.extend(sorted(profiles.glob("*/config.yaml")))
    return [p for p in paths if p.is_file()]


def cmd_yaml_get(path: str, dotted: str) -> int:
    value = yaml_get(Path(path), dotted)
    if value is None:
        return 1
    print(value)
    return 0


def cmd_yaml_list_missing(path: str, dotted: str, *wants: str) -> int:
    have = set(yaml_list(Path(path), dotted))
    for want in wants:
        if not any(alt in have for alt in want.split("|")):
            print(want.split("|")[0])
    return 0


def cmd_agents() -> int:
    try:
        data = json.loads(Path("data/hermes/agents/registry.json").read_text())
    except (OSError, ValueError):
        return 0
    for agent in data.get("agents", []) if isinstance(data, dict) else []:
        if isinstance(agent, dict) and agent.get("name"):
            print(agent["name"])
    return 0


def cmd_corpus_dim(registry: str, slug: str) -> int:
    data = json.loads(Path(registry).read_text())
    for corpus in data.get("corpora", []):
        if corpus.get("slug") == slug:
            print(corpus.get("embedding_dim") or "")
            break
    return 0


def cmd_nested_binds(projects: str) -> int:
    try:
        cfg = json.load(sys.stdin)
    except ValueError:
        return 0
    proj = Path(projects)
    for vol in cfg.get("services", {}).get("hermes", {}).get("volumes", []):
        target = vol.get("target", "")
        if vol.get("type") == "bind" and target.startswith("/opt/projects/"):
            rel = target[len("/opt/projects/") :]
            if not (proj / rel).is_dir():
                print(f"{target} needs {proj / rel}")
    return 0


def cmd_terminal_backend(want: str) -> int:
    configs = _configs()
    if not configs:
        print("NOCONFIG")
        return 0
    for path in configs:
        got = yaml_get(path, "terminal.backend") or ""
        if got != want:
            label = "default" if path.parent.name == "hermes" else path.parent.name
            print(f"{label} terminal.backend is {got or 'unset'}")
    return 0


def cmd_override_mounts(path: str, service: str, *flags: str) -> int:
    host_only = "--host-only" in flags
    for item in yaml_list(Path(path), f"services.{service}.volumes"):
        if not host_only:
            print(item)
            continue
        src = item.split(":", 1)[0]
        if src.startswith("/"):
            print(src)
    return 0


def cmd_proxy_mismatch(want: str) -> int:
    for cfg in [Path("data/hermes/config.yaml")] + sorted(
        Path("data/hermes/profiles").glob("*/config.yaml")
    ):
        if cfg.is_file() and yaml_get(cfg, "model.base_url") != want:
            print(f"hermes {cfg}")
    local = Path("opencode/opencode.local.json")
    if local.is_file():
        try:
            data = json.loads(local.read_text())
        except json.JSONDecodeError:
            print("opencode/opencode.local.json")
        else:
            for name, block in (data.get("provider") or {}).items():
                options = block.get("options") if isinstance(block, dict) else None
                url = options.get("baseURL") if isinstance(options, dict) else None
                if url != want:
                    print(f"opencode provider {name}")
    return 0


COMMANDS = {
    "yaml-get": (cmd_yaml_get, 2, 2),
    "yaml-list-missing": (cmd_yaml_list_missing, 3, None),
    "agents": (cmd_agents, 0, 0),
    "corpus-dim": (cmd_corpus_dim, 2, 2),
    "nested-binds": (cmd_nested_binds, 1, 1),
    "terminal-backend": (cmd_terminal_backend, 1, 1),
    "override-mounts": (cmd_override_mounts, 2, 3),
    "proxy-mismatch": (cmd_proxy_mismatch, 1, 1),
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        sys.stderr.write(__doc__ or "")
        return 2
    fn, lo, hi = COMMANDS[argv[0]]
    args = argv[1:]
    if len(args) < lo or (hi is not None and len(args) > hi):
        sys.stderr.write(f"doctor_checks.py {argv[0]}: wrong number of arguments\n")
        return 2
    return fn(*args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
