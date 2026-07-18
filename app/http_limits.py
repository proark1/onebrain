"""Streaming request-size enforcement shared by HTTP middleware."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import re
from typing import Any

Receive = Callable[[], Awaitable[dict[str, Any]]]
_DRIVE_UPLOAD_CONTENT_PATH = re.compile(r"^/api/drive/uploads/[A-Za-z0-9_-]{8,128}/content$")


class RequestBodyTooLargeError(Exception):
    """Raised before an ASGI request body exceeds its configured byte limit."""


def request_body_limit(
    method: str,
    path: str,
    *,
    default_bytes: int,
    drive_file_bytes: int,
) -> int:
    """Select the larger Drive cap only for the opaque raw-content PUT route."""

    if method.upper() == "PUT" and _DRIVE_UPLOAD_CONTENT_PATH.fullmatch(path or ""):
        return max(1, int(drive_file_bytes))
    return max(1, int(default_bytes))


def limited_receive(receive: Receive, *, max_body_bytes: int) -> Receive:
    """Wrap ``receive`` with a cumulative, streaming body limit.

    Chunked requests have no trustworthy Content-Length header, so rejecting only
    declared sizes leaves the API open to an unbounded upload. This wrapper counts
    chunks as the endpoint consumes them and never buffers their contents.
    """

    consumed = 0

    async def _receive() -> dict[str, Any]:
        nonlocal consumed
        message = await receive()
        if message.get("type") == "http.request":
            body = message.get("body", b"")
            consumed += len(body)
            if consumed > max_body_bytes:
                raise RequestBodyTooLargeError("Request body exceeds configured limit")
        return message

    return _receive
