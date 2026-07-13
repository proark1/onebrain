"""BK4: the object-store seam — Fake (in-memory) + real SigV4 (injected-opener) + pure signer.
No live S3 anywhere; the real transport is exercised only via a capturing opener, and the
SigV4 algorithm is pinned by a known-answer vector."""

from __future__ import annotations

import datetime
import hashlib
import io
import urllib.error

import pytest

from app.config import Settings
from app.fleet.object_store import (
    FakeObjectStore,
    ObjectStoreError,
    SigV4ObjectStore,
    build_object_store,
)
from app.fleet.sigv4 import sign_s3_request


class _Resp:
    def __init__(self, body: bytes = b""):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


# --- FakeObjectStore ---------------------------------------------------------
def test_fake_object_store_roundtrip_prefix_delete_and_fail_on():
    store = FakeObjectStore()
    store.put_object("mc/a.enc", b"AAA")
    store.put_object("mc/b.enc", b"BB")
    store.put_object("cust_x/c.enc", b"C")
    assert store.get_object("mc/a.enc") == b"AAA"
    listed = store.list_objects("mc/")
    assert [o.key for o in listed] == ["mc/a.enc", "mc/b.enc"]      # prefix-filtered + sorted
    assert listed[0].size == 3
    store.delete_object("mc/a.enc")
    assert "mc/a.enc" not in store.objects
    with pytest.raises(ObjectStoreError):
        store.get_object("mc/a.enc")                                # gone -> 404
    boom = FakeObjectStore(fail_on={"put_object"})
    with pytest.raises(ObjectStoreError):
        boom.put_object("x", b"y")


# --- SigV4 known answer (locks the algorithm; catches drift) -----------------
def test_sigv4_known_answer():
    payload = hashlib.sha256(b"hello").hexdigest()
    url, headers = sign_s3_request(
        "PUT", "https://fsn1.your-objectstorage.com", "ob-backups", "mc/2026-07-13.sql.enc",
        "fsn1", "AKIAEXAMPLE", "secret-key-example",
        payload_sha256=payload, now=datetime.datetime(2026, 7, 13, 12, 0, 0))
    assert url == "https://fsn1.your-objectstorage.com/ob-backups/mc/2026-07-13.sql.enc"
    assert headers["x-amz-date"] == "20260713T120000Z"
    assert headers["x-amz-content-sha256"] == payload              # full-payload, never UNSIGNED
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/20260713/fsn1/s3/aws4_request, "
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date, "
        "Signature=754c5235fe289545b512244c07315c2ab5af649921c5b387bf3a2e54d6ab9134")
    # a LIST prefix's "/" is %2F-encoded per SigV4 (and the sent URL matches the signed query)
    lurl, _ = sign_s3_request(
        "GET", "https://fsn1.your-objectstorage.com", "ob-backups", "", "fsn1", "AK", "sk",
        payload_sha256=hashlib.sha256(b"").hexdigest(),
        query={"list-type": "2", "prefix": "mc/"}, now=datetime.datetime(2026, 7, 13, 12, 0, 0))
    assert lurl == "https://fsn1.your-objectstorage.com/ob-backups?list-type=2&prefix=mc%2F"


# --- SigV4ObjectStore request shapes (capturing opener) ----------------------
_LIST_XML = (
    b'<?xml version="1.0"?>'
    b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b'<Contents><Key>mc/2026-07-13.sql.enc</Key><Size>42</Size>'
    b'<LastModified>2026-07-13T12:00:00.000Z</LastModified></Contents>'
    b'<Contents><Key>mc/2026-07-12.sql.enc</Key><Size>40</Size>'
    b'<LastModified>2026-07-12T12:00:00.000Z</LastModified></Contents>'
    b'</ListBucketResult>')


def test_sigv4_store_put_get_list_delete_shapes():
    seen = {}

    def opener(request, timeout):
        seen["method"] = request.get_method()
        seen["url"] = request.full_url
        seen["auth"] = request.headers.get("Authorization")
        seen["sha"] = request.headers.get("X-amz-content-sha256")   # urllib capitalizes header keys
        seen["body"] = request.data
        return _Resp(_LIST_XML if "list-type" in request.full_url else b"")

    store = SigV4ObjectStore(endpoint="https://fsn1.your-objectstorage.com", bucket="ob-backups",
                             region="fsn1", access_key="AK", secret_key="SK", opener=opener)
    # PUT — full-payload signature over the exact bytes
    store.put_object("mc/x.enc", b"payload-bytes")
    assert seen["method"] == "PUT"
    assert seen["url"] == "https://fsn1.your-objectstorage.com/ob-backups/mc/x.enc"
    assert seen["auth"].startswith("AWS4-HMAC-SHA256 Credential=AK/")
    assert seen["sha"] == hashlib.sha256(b"payload-bytes").hexdigest()
    assert seen["body"] == b"payload-bytes"
    # LIST — signed query, XML parsed into sorted ObjectInfos
    infos = store.list_objects("mc/")
    assert seen["method"] == "GET" and "list-type=2&prefix=mc%2F" in seen["url"]
    assert [o.key for o in infos] == ["mc/2026-07-12.sql.enc", "mc/2026-07-13.sql.enc"]
    assert infos[1].size == 42 and infos[1].last_modified == "2026-07-13T12:00:00.000Z"
    # DELETE
    store.delete_object("mc/x.enc")
    assert seen["method"] == "DELETE"


def test_sigv4_store_maps_http_error():
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", None, io.BytesIO(b"AccessDenied"))

    store = SigV4ObjectStore(endpoint="https://fsn1.your-objectstorage.com", bucket="b",
                             region="fsn1", access_key="AK", secret_key="SK", opener=opener)
    with pytest.raises(ObjectStoreError) as ei:
        store.get_object("k")
    assert ei.value.status == 403 and "AccessDenied" in ei.value.body


# --- build_object_store: inert default + EU fail-closed ----------------------
def test_build_object_store_inert_and_eu_fail_closed():
    assert build_object_store(Settings()) is None                  # not configured -> inert
    eu = Settings(backup_enabled=True, backup_object_store_region="fsn1",
                  backup_object_store_endpoint="https://fsn1.your-objectstorage.com",
                  backup_object_store_bucket="b", backup_object_store_access_key="AK",
                  backup_object_store_secret_key="SK")
    assert isinstance(build_object_store(eu), SigV4ObjectStore)
    offshore = Settings(backup_enabled=True, backup_object_store_region="us-east-1",
                        backup_object_store_endpoint="https://s3.us-east-1.amazonaws.com",
                        backup_object_store_bucket="b", backup_object_store_access_key="AK",
                        backup_object_store_secret_key="SK")
    with pytest.raises(ValueError, match="not an approved EU endpoint"):
        build_object_store(offshore)                               # constructs no store, fails closed
