"""Real embeddings via LiteLLM (any provider behind one interface).

Requires `pip install litellm` and the relevant provider API key. Imported
lazily so the base app has no dependency on it.
"""

from __future__ import annotations

from typing import List

import numpy as np


class LiteLLMEmbedder:
    name = "litellm"

    def __init__(self, model: str, dim: int = 1024):
        import litellm

        self._litellm = litellm
        self.model = model
        self.dim = dim

    def _embed(self, texts: List[str]) -> np.ndarray:
        response = self._litellm.embedding(model=self.model, input=texts)
        data = response["data"] if isinstance(response, dict) else response.data
        rows = [item["embedding"] if isinstance(item, dict) else item.embedding for item in data]
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
