"""Real embeddings via LiteLLM with a fixed, verified vector contract.

``embedding_dim`` is part of a deployment's persisted pgvector schema.  It
must therefore be configured deliberately, passed to the provider, and never
silently inferred from a response at runtime.
"""

from __future__ import annotations

from typing import Any, List

import numpy as np

# Providers cap how many inputs one embedding request may carry. Gemini's
# batchEmbedContents allows at most 100; 100 is safe for every provider.
_MAX_BATCH = 100


class EmbeddingProviderError(RuntimeError):
    """The configured embedding provider cannot satisfy the deployment contract."""


class LiteLLMEmbedder:
    name = "litellm"

    def __init__(self, model: str, dim: int):
        if not model.strip():
            raise ValueError("A LiteLLM embedding model must be configured.")
        if dim <= 0:
            raise ValueError("ONEBRAIN_EMBEDDING_DIM must be a positive integer.")

        import litellm

        self._litellm = litellm
        self.model = model
        self.dim = dim

    def probe(self) -> None:
        """Verify credentials, reachability, and the configured output dimension."""
        self._embed(["onebrain embedding provider preflight"])

    def _embed(self, texts: List[str]) -> np.ndarray:
        rows: list[np.ndarray] = []
        for start in range(0, len(texts), _MAX_BATCH):
            batch = texts[start:start + _MAX_BATCH]
            try:
                response = self._litellm.embedding(
                    model=self.model,
                    input=batch,
                    dimensions=self.dim,
                )
                data = response["data"] if isinstance(response, dict) else response.data
            except Exception as exc:
                raise EmbeddingProviderError(
                    f"LiteLLM embedding preflight failed for model {self.model!r}. "
                    "Check provider credentials, network access, and dimensions support."
                ) from exc

            if len(data) != len(batch):
                raise EmbeddingProviderError(
                    f"LiteLLM returned {len(data)} embeddings for {len(batch)} inputs."
                )
            for item in data:
                vector = item["embedding"] if isinstance(item, dict) else item.embedding
                array = np.asarray(vector, dtype=np.float32)
                if array.ndim != 1 or array.shape[0] != self.dim:
                    actual = array.shape[0] if array.ndim == 1 else tuple(array.shape)
                    raise EmbeddingProviderError(
                        "LiteLLM embedding dimension mismatch: "
                        f"configured {self.dim}, provider returned {actual}. "
                        "Refusing to mix incompatible vectors."
                    )
                rows.append(array)

        vecs = np.vstack(rows)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vecs / norms

    def embed(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return self._embed(texts)

    def embed_one(self, text: str) -> np.ndarray:
        return self._embed([text])[0]
