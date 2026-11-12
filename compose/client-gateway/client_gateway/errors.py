"""JSON-RPC errors as the gateway sends them.

Every error carries a string reason under the stack namespace in ``data``, so
clients branch on ``data["io.github.kaxline.stack"]["reason"]`` rather than on a
runtime's numeric codes, which never leave the gateway.
"""

from __future__ import annotations

NS = "io.github.kaxline.stack"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
AUTH_REQUIRED = -32000
RESOURCE_NOT_FOUND = -32002
REQUEST_CANCELLED = -32800


class AcpError(Exception):
    """An error a handler raises to answer its request with."""

    def __init__(self, code: int, message: str, reason: str | None = None, **extra: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.extra = extra

    def to_error(self) -> dict:
        return error_obj(self.code, self.message, self.reason, **self.extra)


def error_obj(code: int, message: str, reason: str | None = None, **extra: object) -> dict:
    err: dict = {"code": code, "message": message}
    if reason is not None or extra:
        body: dict = dict(extra)
        if reason is not None:
            body["reason"] = reason
        err["data"] = {NS: body}
    return err


def reason_of(error: dict) -> str | None:
    """The stack reason in an error object, or None."""
    data = error.get("data")
    if isinstance(data, dict) and isinstance(data.get(NS), dict):
        return data[NS].get("reason")
    return None
