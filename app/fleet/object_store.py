"""The object-store seam the restore tool (BK7) and any MC-side backup run against.

`FakeObjectStore` (in-memory) makes upload/download unit-testable with NO network; the real
`SigV4ObjectStore` talks to any S3-compatible store (Hetzner Object Storage) via stdlib urllib
and an INJECTABLE opener — the exact seam as `UrllibHetznerClient`, so tests assert request
shape without a live account. Nothing here is constructed at app startup: `build_object_store`
returns None until a bucket is configured, so the whole layer stays dormant."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from app.fleet.sigv4 import EMPTY_PAYLOAD_SHA256, sign_s3_request

_TIMEOUT_SECONDS = 30


class ObjectStoreError(RuntimeError):
    """A non-2xx / transport error from the object store. Carries a truncated, credential-free
    body — the caller (backup agent / restore tool) redacts before logging."""

    def __init__(self, status: int, body: str = ""):
        self.status = int(status)
        self.body = (body or "")[:500]
        super().__init__(f"object store error ({self.status}): {self.body}")


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    last_modified: str            # ISO8601 from the S3 LIST response ("" if absent)


class ObjectStore(Protocol):
    def put_object(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None: ...
    def get_object(self, key: str) -> bytes: ...
    def list_objects(self, prefix: str) -> list[ObjectInfo]: ...
    def delete_object(self, key: str) -> None: ...


class FakeObjectStore:
    """In-memory object store for tests. `.objects` is {key: bytes}; `.calls` is the ordered
    method log; `fail_on={"put_object", ...}` raises ObjectStoreError for that method."""

    def __init__(self, *, fail_on=frozenset()):
        self.objects: dict = {}
        self.calls: list = []
        self.fail_on = set(fail_on)

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail_on:
            raise ObjectStoreError(500, f"injected failure: {method}")

    def put_object(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
        self.calls.append("put_object")
        self._maybe_fail("put_object")
        self.objects[key] = bytes(data)

    def get_object(self, key: str) -> bytes:
        self.calls.append("get_object")
        self._maybe_fail("get_object")
        if key not in self.objects:
            raise ObjectStoreError(404, f"no such key: {key}")
        return self.objects[key]

    def list_objects(self, prefix: str) -> list[ObjectInfo]:
        self.calls.append("list_objects")
        self._maybe_fail("list_objects")
        return [
            ObjectInfo(key=k, size=len(v), last_modified="")
            for k, v in sorted(self.objects.items()) if k.startswith(prefix)
        ]

    def delete_object(self, key: str) -> None:
        self.calls.append("delete_object")
        self._maybe_fail("delete_object")
        self.objects.pop(key, None)


def _localname(tag: str) -> str:
    # Strip the XML namespace ({http://s3.amazonaws.com/...}Key -> Key).
    return tag.rsplit("}", 1)[-1]


class SigV4ObjectStore:
    """Real S3-compatible store. Path-style, SigV4-signed via app.fleet.sigv4, stdlib urllib with
    an injectable opener (tests need no network). Never reads globals — endpoint/bucket/region +
    creds are passed in by build_object_store."""

    def __init__(self, *, endpoint: str, bucket: str, region: str,
                 access_key: str, secret_key: str, opener=None):
        self._endpoint = endpoint.rstrip("/")
        self._bucket = bucket
        self._region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._opener = opener

    def _do_open(self, request: Request):
        do_open = self._opener or (lambda req, timeout: urlopen(req, timeout=timeout))
        return do_open(request, _TIMEOUT_SECONDS)

    @staticmethod
    def _error_body(exc: HTTPError) -> str:
        try:
            return exc.read().decode("utf-8", "replace")
        except Exception:
            return getattr(exc, "reason", "") or ""

    def _send(self, method: str, key: str, *, payload_sha256: str, body=None,
              query=None, extra_headers=None) -> bytes:
        url, headers = sign_s3_request(
            method, self._endpoint, self._bucket, key, self._region,
            self._access_key, self._secret_key, payload_sha256=payload_sha256, query=query)
        if extra_headers:
            headers.update(extra_headers)
        request = Request(url, data=body, method=method, headers=headers)
        try:
            with self._do_open(request) as response:
                return response.read()
        except HTTPError as exc:
            raise ObjectStoreError(exc.code, self._error_body(exc)) from exc
        except URLError as exc:
            raise ObjectStoreError(0, str(getattr(exc, "reason", exc))) from exc

    def put_object(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
        # Full-payload signature: hash the exact bytes we send (never UNSIGNED-PAYLOAD).
        self._send("PUT", key, payload_sha256=hashlib.sha256(data).hexdigest(),
                   body=data, extra_headers={"Content-Type": content_type})

    def get_object(self, key: str) -> bytes:
        return self._send("GET", key, payload_sha256=EMPTY_PAYLOAD_SHA256)

    def delete_object(self, key: str) -> None:
        self._send("DELETE", key, payload_sha256=EMPTY_PAYLOAD_SHA256)

    def list_objects(self, prefix: str) -> list[ObjectInfo]:
        # ListObjectsV2, bucket-level (key=""); the prefix rides the (signed) query string.
        raw = self._send("GET", "", payload_sha256=EMPTY_PAYLOAD_SHA256,
                         query={"list-type": "2", "prefix": prefix})
        out: list[ObjectInfo] = []
        root = ElementTree.fromstring(raw) if raw else None
        for child in (root if root is not None else []):
            if _localname(child.tag) != "Contents":
                continue
            fields = {_localname(g.tag): (g.text or "") for g in child}
            out.append(ObjectInfo(
                key=fields.get("Key", ""),
                size=int(fields.get("Size", "0") or "0"),
                last_modified=fields.get("LastModified", ""),
            ))
        return sorted(out, key=lambda o: o.key)


def build_object_store(settings) -> Optional[ObjectStore]:
    """Construct the real store from ONEBRAIN_BACKUP_* settings, or None when not fully
    configured (callers stay inert). When backups are ENABLED, FIRST assert the endpoint is an
    approved EU host (BK3, fail closed) — a non-EU endpoint constructs no store and raises."""
    if not getattr(settings, "backup_object_store_configured", False):
        return None
    settings.assert_backup_endpoint_eu()      # BK3: GDPR residency gate, fail closed
    return SigV4ObjectStore(
        endpoint=settings.backup_object_store_endpoint,
        bucket=settings.backup_object_store_bucket,
        region=settings.backup_object_store_region,
        access_key=settings.backup_object_store_access_key,
        secret_key=settings.backup_object_store_secret_key,
    )
