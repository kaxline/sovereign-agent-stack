"""Summarize an MCP tools/list response read from a file.

Usage: python3 mcp_tools_list.py <response-file> [required-tool ...]

The response is a JSON-RPC body or a server-sent event stream whose data
lines hold one. Prints tab-separated lines, never tool descriptions:

    count<TAB><number of tools>
    missing<TAB><required tools the server doesn't list, space-separated>
    error<TAB><why the response couldn't be read>

crm.sh runs it in the Hermes image (stack_python), doctor on the host.
"""

import json
import sys


def _messages(text: str):
    text = text.strip()
    if text.startswith("{"):
        yield json.loads(text)
        return
    for line in text.splitlines():
        if line.startswith("data:"):
            data = line[5:].strip()
            if data.startswith("{"):
                yield json.loads(data)


def summarize(text: str, required: list[str]) -> list[str]:
    try:
        for msg in _messages(text):
            if not isinstance(msg, dict):
                continue
            if "error" in msg:
                err = msg["error"] if isinstance(msg["error"], dict) else {}
                return [f"error\ttools/list failed: {str(err.get('message', 'unknown error'))[:200]}"]
            tools = (msg.get("result") or {}).get("tools")
            if isinstance(tools, list):
                names = {t.get("name") for t in tools if isinstance(t, dict)}
                missing = [n for n in required if n not in names]
                return [f"count\t{len(tools)}", "missing\t" + " ".join(missing)]
    except (ValueError, AttributeError):
        return ["error\tthe tools/list answer isn't JSON-RPC"]
    return ["error\tthe tools/list answer has no tool list"]


def main() -> int:
    if len(sys.argv) < 2:
        print("error\tusage: mcp_tools_list.py <response-file> [required-tool ...]")
        return 0
    try:
        with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        print(f"error\tcan't read the response: {e.strerror}")
        return 0
    print("\n".join(summarize(text, sys.argv[2:])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
