from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.auth.principal import Principal
from app.drive.base import DriveFile, DriveRevision, DriveUploadSession
from app.routers import drive as drive_router
from app.security.policy import Classification


ACCOUNT = "tenant_account"
SPACE = "space_shared"


def _principal(*, principal_type: str = "human") -> Principal:
    return Principal(
        user_id="user_owner",
        role_id="admin" if principal_type == "human" else "service",
        role_label="Admin",
        clearance=Classification.RESTRICTED,
        locations=None,
        categories=None,
        location_label="all locations",
        tenant_id=ACCOUNT,
        principal_type=principal_type,
    )


def test_drive_request_models_reject_unknown_or_malformed_fields():
    valid = {
        "account_id": ACCOUNT,
        "space_id": SPACE,
        "name": "handbook.txt",
        "size_bytes": 10,
        "idempotency_key": "request-1",
    }
    assert drive_router.UploadCreateIn(**valid).size_bytes == 10
    with pytest.raises(ValidationError):
        drive_router.UploadCreateIn(**valid, caller_supplied_owner="attacker")
    with pytest.raises(ValidationError):
        drive_router.UploadCreateIn(**{**valid, "size_bytes": 0})


def test_drive_requires_a_human_employee_not_a_service_key():
    assert drive_router._human(_principal()).is_employee
    with pytest.raises(HTTPException) as error:
        drive_router._human(_principal(principal_type="service"))
    assert error.value.status_code == 403


def test_api_serializers_do_not_expose_blob_or_private_owner_keys():
    file = DriveFile(
        id="file_aaaaaaaa",
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        folder_id="",
        name="handbook.txt",
        owner_user_id="user_owner",
        current_revision_id="revision_aaaaaaaa",
        uploaded_by="user_owner",
    )
    revision = DriveRevision(
        id="revision_aaaaaaaa",
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        file_id=file.id,
        upload_session_id="upload_aaaaaaaa",
        storage_key="drive/private/storage/path",
        sha256="a" * 64,
        size_bytes=42,
        media_type="text/plain",
        original_name=file.name,
        created_by="user_owner",
    )
    service = SimpleNamespace(store=SimpleNamespace(get_revision=lambda *args, **kwargs: revision))
    output = drive_router._file_out(file, service)
    assert output["size_bytes"] == 42
    assert output["download_url"].startswith("/api/drive/files/")
    assert "storage_key" not in output
    assert "owner_user_id" not in output
    assert "tenant_id" not in output

    upload = DriveUploadSession(
        id="upload_aaaaaaaa",
        tenant_id=ACCOUNT,
        account_id=ACCOUNT,
        space_id=SPACE,
        folder_id="",
        name="handbook.txt",
        size_bytes=42,
        desired_indexed=True,
        classification="internal",
        location="global",
        category="general",
        created_by="user_owner",
        idempotency_key="request-1",
        staging_key="staging/private-key",
    )
    assert "staging_key" not in drive_router._upload_out(upload)


def test_download_supports_bounded_byte_ranges_and_security_headers(monkeypatch):
    payload = b"0123456789"
    digest = hashlib.sha256(payload).hexdigest()
    file = SimpleNamespace(name="policy final.txt")
    revision = SimpleNamespace(storage_key="opaque", sha256=digest)

    class Blobs:
        @staticmethod
        def stat(_key):
            return SimpleNamespace(size_bytes=len(payload))

        @staticmethod
        def iter_range(_key, *, start, end):
            yield payload[start:end + 1]

    service = SimpleNamespace(
        blobs=Blobs(),
        get_revision_for_download=lambda *args, **kwargs: (file, revision),
    )
    monkeypatch.setattr(drive_router, "get_drive_service", lambda: service)

    response = drive_router.download_file(
        "file_aaaaaaaa",
        ACCOUNT,
        SPACE,
        range_header="bytes=2-5",
        principal=_principal(),
    )

    async def body():
        return b"".join([chunk async for chunk in response.body_iterator])

    assert response.status_code == 206
    assert asyncio.run(body()) == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["etag"] == f'"{digest}"'

    with pytest.raises(HTTPException) as error:
        drive_router.download_file(
            "file_aaaaaaaa",
            ACCOUNT,
            SPACE,
            range_header="bytes=99-100",
            principal=_principal(),
        )
    assert error.value.status_code == 416
    assert error.value.headers == {"Content-Range": "bytes */10"}


def test_review_view_authorizes_space_membership_before_reading_pending_metadata(monkeypatch):
    calls: list[tuple[str, str]] = []

    class Service:
        store = SimpleNamespace(list_pending_review=lambda **kwargs: [])

        @staticmethod
        def authorize_space(_principal, account_id, space_id):
            calls.append((account_id, space_id))

    monkeypatch.setattr(drive_router, "get_drive_service", Service)

    result = drive_router.list_items(
        account_id=ACCOUNT,
        space_id=SPACE,
        view="review",
        principal=_principal(),
    )

    assert result == {"entries": [], "next_cursor": None}
    assert calls == [(ACCOUNT, SPACE)]
