from __future__ import annotations

import asyncio

import pytest

from app.http_limits import RequestBodyTooLargeError, limited_receive


def test_limited_receive_counts_chunked_request_bodies_without_buffering():
    messages = iter([
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"de", "more_body": True},
    ])

    async def receive():
        return next(messages)

    async def exercise():
        bounded = limited_receive(receive, max_body_bytes=4)
        assert await bounded() == {"type": "http.request", "body": b"abc", "more_body": True}
        with pytest.raises(RequestBodyTooLargeError):
            await bounded()

    asyncio.run(exercise())
