"""LLM interface — streams answer tokens from permission-filtered context."""

from __future__ import annotations

from typing import Iterator, List, Protocol

from app.store.base import Hit


class LLM(Protocol):
    name: str

    def stream(self, question: str, hits: List[Hit]) -> Iterator[str]: ...
