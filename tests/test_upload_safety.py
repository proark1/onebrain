from __future__ import annotations

import asyncio

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
        raise AssertionError("retired upload content must never be read")


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


def test_legacy_upload_is_retired_before_content_or_ingestion(monkeypatch):
    monkeypatch.setattr(documents_router, "get_platform_store", lambda: object())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(documents_router.upload(
            file=FakeUpload(b"must not be extracted"),
            classification="internal",
            location="global",
            category="general",
            account_id="",
            space_id="",
            principal=_employee(),
        ))

    assert exc.value.status_code == 410
    assert "/api/drive/uploads" in str(exc.value.detail)
    assert exc.value.headers == {"Link": "</drive>; rel=\"alternate\""}
