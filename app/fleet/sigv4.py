"""Pure, stdlib-only AWS Signature Version 4 request signer for S3 (path-style).

No app imports, no globals, no network, no clock read unless `now` is omitted — deterministic
given `now`, so it is unit-testable against a KNOWN ANSWER and safe to copy verbatim to the box
(BK5's onebrain_s3.py mirrors this exact algorithm). Reference: the AWS SigV4 signing process.

Path-style (`{endpoint}/{bucket}/{key}`) so it works against S3-compatible stores (Hetzner
Object Storage) that don't do virtual-host addressing. The payload hash is ALWAYS a real
SHA256 (never `UNSIGNED-PAYLOAD`), so a caller must hash the full body first (BK5 two-passes a
file: hash, then stream)."""

from __future__ import annotations

import datetime
import hashlib
import hmac
from urllib.parse import quote, urlsplit

_ALG = "AWS4-HMAC-SHA256"
_SERVICE = "s3"
# SHA256 of the empty byte string — the payload hash for bodyless requests (GET/DELETE/LIST).
EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"").hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, datestamp: str, region: str) -> bytes:
    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, _SERVICE)
    return _hmac(k_service, "aws4_request")


def _canonical_query(query) -> str:
    # SigV4: params sorted by key, each key and value URI-encoded (unreserved only -> "/" becomes
    # %2F). The ACTUAL request query must equal this canonical string, so callers use the returned
    # url (which carries it) verbatim.
    if not query:
        return ""
    items = sorted((quote(str(k), safe="~"), quote(str(v), safe="~")) for k, v in query.items())
    return "&".join(f"{k}={v}" for k, v in items)


def sign_s3_request(method, endpoint, bucket, key, region, access_key, secret_key, *,
                    payload_sha256, query=None, now=None):
    """Return ``(url, headers)`` for a SigV4-signed path-style S3 request.

    ``payload_sha256`` is the hex SHA256 of the EXACT request body (``EMPTY_PAYLOAD_SHA256`` for
    bodyless requests). ``now`` (a UTC datetime) makes the signature deterministic for tests;
    omitted, it reads the wall clock. ``key`` may be "" for a bucket-level request (LIST)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    host = urlsplit(endpoint).netloc

    canonical_uri = "/" + bucket + ("/" + quote(key, safe="/~") if key else "")
    canonical_qs = _canonical_query(query)
    # S3 requires host + x-amz-content-sha256 + x-amz-date in the signature (already sorted).
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_sha256}\n"
        f"x-amz-date:{amzdate}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        [method, canonical_uri, canonical_qs, canonical_headers, signed_headers, payload_sha256])

    scope = f"{datestamp}/{region}/{_SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [_ALG, amzdate, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    signature = hmac.new(
        _signing_key(secret_key, datestamp, region),
        string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"{_ALG} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}")
    url = f"{endpoint.rstrip('/')}{canonical_uri}"
    if canonical_qs:
        url += "?" + canonical_qs
    headers = {
        "Authorization": authorization,
        "x-amz-content-sha256": payload_sha256,
        "x-amz-date": amzdate,
        "Host": host,
    }
    return url, headers
