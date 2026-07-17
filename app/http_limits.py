"""Streaming request-size enforcement shared by HTTP middleware."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

Receive = Callable[[], Awaitable[dict[str, Any]]]


class RequestBodyTooLargeError(Exception):
    """Raised before an ASGI request body exceeds its configured byte limit."""


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
