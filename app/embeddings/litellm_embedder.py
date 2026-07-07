"""Real embeddings via LiteLLM (any provider behind one interface).

Requires `pip install litellm` and the relevant provider API key. Imported
lazily so the base app has no dependency on it.
"""

from __future__ import annotations

from typing import List

import numpy as np

# Providers cap how many inputs one embedding request may carry. Gemini's
# batchEmbedContents allows at most 100; 100 is safe for every provider.
_MAX_BATCH = 100


class LiteLLMEmbedder:
    name = "litellm"

    def __init__(self, model: str, dim: int = 1024):
        import litellm

        self._litellm = litellm
        self.model = model
        self.dim = dim
        # Probe the true output dimension once, so a fixed-dim store (pgvector)
        # creates its column at the right size before anything is inserted.
        try:
            self._embed(["dimension probe"])  # updates self.dim as a side effect
        except Exception:  # no key / offline — corrected on first real embed
            pass

    def _embed(self, texts: List[str]) -> np.ndarray:
        rows: list = []
        for start in range(0, len(texts), _MAX_BATCH):
            batch = texts[start:start + _MAX_BATCH]
            response = self._litellm.embedding(model=self.model, input=batch)
            data = response["data"] if isinstance(response, dict) else response.data
            rows.extend(item["embedding"] if isinstance(item, dict) else item.embedding for item in data)
        vecs = np.array(rows, dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self.dim = vecs.shape[1]
        return vecs / norms

    def embed(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return self._embed(texts)

    def embed_one(self, text: str) -> np.ndarray:
        return self._embed([text])[0]
