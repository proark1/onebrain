"""LLM interface — streams answer tokens from permission-filtered context."""

from __future__ import annotations

from typing import Iterator, List, Optional, Protocol

from app.store.base import Hit


class LLM(Protocol):
    name: str

    def stream(
        self, question: str, hits: List[Hit], tenant_id: str = "nft_gym",
        stats: Optional[dict] = None,
    ) -> Iterator[str]:
        """Yield answer tokens. If `stats` is given, populate it after streaming
        with prompt_tokens / completion_tokens / cost_usd where known."""
        ...
