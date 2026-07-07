"""Pick the embedder from config."""

from __future__ import annotations

from app.config import Settings
from app.embeddings.local import LocalEmbedder


def build_embedder(settings: Settings):
    if settings.embeddings_provider == "litellm":
        from app.embeddings.litellm_embedder import LiteLLMEmbedder

        return LiteLLMEmbedder(settings.litellm_embedding_model)
    return LocalEmbedder(dim=settings.embedding_dim)
