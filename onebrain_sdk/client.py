"""OneBrain service client.

This package is intentionally tiny: external tools only need a base URL and a
scoped service key to capture data or ask the brain for a public-ceiled answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


Transport = Callable[[str, str, Mapping[str, str], dict[str, Any] | None, float], dict[str, Any]]


@dataclass
class OneBrainError(RuntimeError):
    status_code: int
    detail: str

    def __str__(self) -> str:
        return f"OneBrain request failed ({self.status_code}): {self.detail}"


class OneBrainClient:
    def __init__(
        self,
        base_url: str,
        service_key: str,
        *,
        account_id: str = "",
        space_id: str = "",
        app_id: str = "",
        purpose: str = "",
        timeout: float = 15,
        transport: Transport | None = None,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.service_key = service_key
        self.account_id = account_id
        self.space_id = space_id
        self.app_id = app_id
        self.purpose = purpose
        self.timeout = timeout
        self.transport = transport or self._http_transport

    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "api/service/capabilities")

    def ask(
        self,
        question: str,
        *,
        account_id: str = "",
        space_id: str = "",
        app_id: str = "",
        purpose: str = "",
    ) -> dict[str, Any]:
        return self._request("POST", "api/service/ask", {
            "question": question,
            **self._scope(account_id, space_id, app_id, purpose),
        })

    def search_knowledge(self, question: str, **scope: str) -> dict[str, Any]:
        return self.ask(question, **scope)

    def capture_text(
        self,
        text: str,
        *,
        title: str = "",
        account_id: str = "",
        space_id: str = "",
        app_id: str = "",
        purpose: str = "",
    ) -> dict[str, Any]:
        payload = {"text": text, **self._scope(account_id, space_id, app_id, purpose)}
        if title:
            payload["title"] = title
        return self._request("POST", "api/service/capture", payload)

    def store_message(
        self,
        *,
        channel: str,
        sender: str,
        text: str,
        external_id: str = "",
        title: str = "",
        metadata: Mapping[str, Any] | None = None,
        account_id: str = "",
        space_id: str = "",
        app_id: str = "",
        purpose: str = "customer_service_inbox",
    ) -> dict[str, Any]:
        lines = [
            f"channel: {channel}",
            f"sender: {sender}",
        ]
        if external_id:
            lines.append(f"external_id: {external_id}")
        for key, value in sorted((metadata or {}).items()):
            lines.append(f"{key}: {value}")
        lines.append("")
        lines.append(text)
        return self.capture_text(
            "\n".join(lines),
            title=title or f"{channel} message from {sender}",
            account_id=account_id,
            space_id=space_id,
            app_id=app_id,
            purpose=purpose,
        )

    def _scope(self, account_id: str, space_id: str, app_id: str, purpose: str) -> dict[str, str]:
        scope = {
            "account_id": account_id or self.account_id,
            "space_id": space_id or self.space_id,
            "app_id": app_id or self.app_id,
            "purpose": purpose or self.purpose,
        }
        return {key: value for key, value in scope.items() if value}

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = urljoin(self.base_url, path)
        headers = {
            "Authorization": f"Bearer {self.service_key}",
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        return self.transport(method, url, headers, payload, self.timeout)

    @staticmethod
    def _http_transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(url, data=data, method=method, headers=dict(headers))
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8")
            detail = "Request failed"
            if body:
                try:
                    detail = json.loads(body).get("detail", detail)
                except json.JSONDecodeError:
                    detail = body
            raise OneBrainError(exc.code, detail) from exc
        except URLError as exc:
            raise OneBrainError(0, str(exc.reason)) from exc
        return json.loads(body) if body else {}
