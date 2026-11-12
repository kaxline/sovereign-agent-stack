"""Frame validation against the vendored ACP schema and the stack's own schema.

Definitions in the ACP schema carry ``x-method``; a name ending in ``Response``
is that method's result, any other is its params. Extension methods (names
starting with ``_``) are checked against ``schema/stack.json`` when it defines
them and pass otherwise, as ACP's extensibility rules allow.
"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"


class Validator:
    def __init__(self, schema_dir: Path = SCHEMA_DIR) -> None:
        acp = json.loads((schema_dir / "acp-v1.json").read_text())
        self._defs = acp["$defs"]
        self._params: dict[str, str] = {}
        self._results: dict[str, str] = {}
        for name, d in self._defs.items():
            method = d.get("x-method")
            if not method:
                continue
            (self._results if name.endswith("Response") else self._params)[method] = name
        # stack.json: {"$defs": {...}, "x-methods": {method: {"params": Def, "result": Def}}}.
        # Its definitions share one namespace with ACP's, so they can $ref ACP types.
        stack_path = schema_dir / "stack.json"
        stack = json.loads(stack_path.read_text()) if stack_path.exists() else {}
        clash = set(stack.get("$defs", {})) & set(self._defs)
        if clash:
            raise ValueError(f"stack.json redefines ACP definitions: {sorted(clash)}")
        self._defs = {**self._defs, **stack.get("$defs", {})}
        self._stack_methods: dict = stack.get("x-methods", {})
        self._cache: dict[str, Draft202012Validator] = {}

    def _validator(self, name: str) -> Draft202012Validator:
        if name not in self._cache:
            self._cache[name] = Draft202012Validator({"$defs": self._defs, "$ref": f"#/$defs/{name}"})
        return self._cache[name]

    def _check(self, method: str, kind: str, value: object) -> list[str]:
        if method.startswith("_"):
            name = self._stack_methods.get(method, {}).get(kind)
            if not name:
                return []
        else:
            name = (self._params if kind == "params" else self._results).get(method)
            if not name:
                return [f"no schema for {method} {kind}"]
        v = self._validator(name)
        return [f"{method} {kind}: {e.json_path}: {e.message[:200]}" for e in v.iter_errors(value)]

    def params_errors(self, method: str, params: object) -> list[str]:
        return self._check(method, "params", {} if params is None else params)

    def result_errors(self, method: str, result: object) -> list[str]:
        return self._check(method, "result", result)

    def frame_errors(self, frame: dict, sent_methods: dict | None = None) -> list[str]:
        """Errors for one frame. ``sent_methods`` maps request ids to methods so a
        response can be checked against the request it answers."""
        if frame.get("jsonrpc") != "2.0":
            return ["jsonrpc is not 2.0"]
        if "method" in frame:
            return self.params_errors(frame["method"], frame.get("params"))
        if "result" in frame and sent_methods is not None:
            method = sent_methods.get(frame.get("id"))
            return self.result_errors(method, frame["result"]) if method else []
        if "error" in frame:
            err = frame["error"]
            if not isinstance(err, dict) or not isinstance(err.get("code"), int):
                return ["error object malformed"]
        return []
