from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.routers.documents as documents_router
from app.auth.principal import Principal
from app.auth.roles import ROLES


class FakeUpload:
    def __init__(self, data: bytes, filename: str = "upload.txt"):
        self._data = data
        self._pos = 0
        self.filename = filename

    async def read(self, size: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        if size is None or size < 0:
            size = len(self._data) - self._pos
        start = self._pos
        self._pos = min(len(self._data), self._pos + size)
        return self._data[start:self._pos]


def _employee() -> Principal:
    role = ROLES["admin"]
    return Principal(
        user_id="admin@nft_gym",
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=None,
        categories=role.categories,
        location_label="all locations",
        tenant_id="nft_gym",
    )


def test_read_upload_limited_allows_exact_limit():
    data = asyncio.run(documents_router._read_upload_limited(FakeUpload(b"12345"), 5))

    assert data == b"12345"


def test_read_upload_limited_rejects_actual_bytes_over_limit():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(documents_router._read_upload_limited(FakeUpload(b"123456"), 5))

    assert exc.value.status_code == 413


def test_upload_uses_threadpool_for_ingestion(monkeypatch):
    calls: list[str] = []

    class Pipeline:
        def ingest_file(self, **kwargs):
            calls.append(kwargs["filename"])
            return SimpleNamespace(
                doc_id="doc_1",
                title="upload.txt",
                classification="internal",
                location="global",
                category="general",
                chunks=1,
                status="approved",
                pii_findings=[],
            )

    async def fake_run_in_threadpool(func, *args, **kwargs):
        calls.append("threadpool")
        return func(*args, **kwargs)

    monkeypatch.setattr(documents_router, "get_settings", lambda: SimpleNamespace(
        max_body_bytes=100,
        require_approval=False,
        block_public_on_pii=True,
        pii_phase="synthetic",
    ))
    monkeypatch.setattr(documents_router, "get_pipeline", lambda: Pipeline())
    monkeypatch.setattr(documents_router, "get_platform_store", lambda: object())
    monkeypatch.setattr(documents_router, "run_in_threadpool", fake_run_in_threadpool)

    result = asyncio.run(documents_router.upload(
        file=FakeUpload(b"hello"),
        classification="internal",
        location="global",
        category="general",
        account_id="",
        space_id="",
        principal=_employee(),
    ))

    assert result.doc_id == "doc_1"
    assert calls == ["threadpool", "upload.txt"]
