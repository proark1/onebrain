"""Zero-dependency local embedder: signed hashed bag-of-tokens.

Not as semantically strong as a real embedding model, but deterministic and
free — it lets the whole retrieval + permission path run with no API key. Light
stemming (dropping a trailing 's') helps singular/plural terms match. Swap in
`LiteLLMEmbedder` for production-quality retrieval.
"""

from __future__ import annotations

import hashlib
import re
from typing import List

import numpy as np

_TOKEN = re.compile(r"[a-z0-9]+")


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _tokens(text: str) -> List[str]:
    words = [_stem(w) for w in _TOKEN.findall(text.lower())]
    grams = list(words)
    grams += [f"{a}_{b}" for a, b in zip(words, words[1:])]  # bigrams add a little signal
    return grams


class LocalEmbedder:
    name = "local-hashing"

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for tok in _tokens(text):
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = float(np.linalg.norm(v))
        return v / norm if norm > 0 else v

    def embed(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([self._vec(t) for t in texts])

    def embed_one(self, text: str) -> np.ndarray:
        return self._vec(text)
