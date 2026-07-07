"""Embedder interface — the seam that makes the model swappable."""

from __future__ import annotations

from typing import List, Protocol

import numpy as np


class Embedder(Protocol):
    dim: int
    name: str

    def embed(self, texts: List[str]) -> np.ndarray: ...

    def embed_one(self, text: str) -> np.ndarray: ...
