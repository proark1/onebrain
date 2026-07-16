"""Provider-neutral connector transport and errors."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ConnectorUnavailableError(RuntimeError):
    """A connector is not configured or its credential is unavailable."""


class ConnectorRequestError(RuntimeError):
    """A provider request failed without exposing provider secrets."""

    def __init__(self, message: str, *, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class HttpResult:
    status: int
    payload: dict
    headers: dict[str, str]


class UrllibJsonTransport:
    def request(self, method, url, *, headers=None, body=b"", timeout=10.0) -> HttpResult:
        request = Request(
            url,
            data=body if body else None,
            headers=dict(headers or {}),
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                return HttpResult(
                    status=int(response.status),
                    payload=payload if isinstance(payload, dict) else {},
                    headers=dict(response.headers.items()),
                )
        except HTTPError as exc:
            raise ConnectorRequestError(
                f"External connector request failed with HTTP {exc.code}.",
                status_code=int(exc.code),
            ) from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise ConnectorRequestError("External connector request failed.") from exc
