from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from app.embeddings.litellm_embedder import EmbeddingProviderError, LiteLLMEmbedder


class FakeLiteLLM:
    def __init__(self, vectors: list[list[float]] | None = None, error: Exception | None = None):
        self.vectors = vectors or [[3.0, 4.0]]
        self.error = error
        self.calls: list[dict] = []

    def embedding(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"data": [{"embedding": vector} for vector in self.vectors]}


def _embedder(monkeypatch, fake: FakeLiteLLM, *, dim: int = 2) -> LiteLLMEmbedder:
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return LiteLLMEmbedder("provider/model", dim=dim)


def test_litellm_uses_configured_dimension_without_mutating_it(monkeypatch):
    fake = FakeLiteLLM()
    embedder = _embedder(monkeypatch, fake)

    vector = embedder.embed_one("hello")

    assert embedder.dim == 2
    assert fake.calls == [{"model": "provider/model", "input": ["hello"], "dimensions": 2}]
    np.testing.assert_allclose(vector, np.array([0.6, 0.8], dtype=np.float32))


def test_litellm_rejects_provider_dimension_mismatch(monkeypatch):
    embedder = _embedder(monkeypatch, FakeLiteLLM(vectors=[[1.0, 2.0, 3.0]]))

    with pytest.raises(EmbeddingProviderError, match="dimension mismatch"):
        embedder.probe()

    assert embedder.dim == 2


def test_litellm_provider_errors_are_actionable(monkeypatch):
    embedder = _embedder(monkeypatch, FakeLiteLLM(error=ConnectionError("offline")))

    with pytest.raises(EmbeddingProviderError, match="credentials, network access"):
        embedder.probe()


def test_litellm_checks_response_count(monkeypatch):
    embedder = _embedder(monkeypatch, FakeLiteLLM(vectors=[[1.0, 0.0]]))

    with pytest.raises(EmbeddingProviderError, match="1 embeddings for 2 inputs"):
        embedder.embed(["a", "b"])
