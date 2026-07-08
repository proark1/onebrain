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

    def intake(
        self,
        content: str,
        *,
        title: str = "",
        source: str = "service",
        source_ref: str = "",
        record_type: str = "",
        intent: str = "",
        metadata: Mapping[str, Any] | None = None,
        account_id: str = "",
        space_id: str = "",
        app_id: str = "",
        purpose: str = "",
    ) -> dict[str, Any]:
        payload = {
            "content": content,
            "source": source,
            "source_ref": source_ref,
            "record_type": record_type,
            "intent": intent,
            "metadata": dict(metadata or {}),
            **self._scope(account_id, space_id, app_id, purpose),
        }
        if title:
            payload["title"] = title
        return self._request("POST", "api/service/intake", payload)

    def create_assistant_record(
        self,
        content: str,
        *,
        record_type: str,
        title: str = "",
        intent: str = "",
        source: str = "assistant",
        source_ref: str = "",
        purpose: str = "assistant_context",
        metadata: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
        retention: Mapping[str, Any] | None = None,
        account_id: str = "",
        space_id: str = "",
    ) -> dict[str, Any]:
        payload = {
            "content": content,
            "record_type": record_type,
            "intent": intent,
            "source": source,
            "source_ref": source_ref,
            "purpose": purpose,
            "metadata": dict(metadata or {}),
            "provenance": dict(provenance or {}),
            "retention": dict(retention or {}),
            **self._account_space_scope(account_id, space_id),
        }
        if title:
            payload["title"] = title
        return self._request("POST", "api/service/assistant/records", payload)

    def get_assistant_record(self, record_id: str) -> dict[str, Any]:
        return self._request("GET", f"api/service/assistant/records/{record_id}")

    def record_assistant_audit(
        self,
        *,
        action: str,
        target_type: str,
        target_id: str,
        purpose: str = "assistant_action",
        decision: str = "recorded",
        metadata: Mapping[str, Any] | None = None,
        account_id: str = "",
        space_id: str = "",
    ) -> dict[str, Any]:
        payload = {
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "purpose": purpose,
            "decision": decision,
            "metadata": dict(metadata or {}),
            **self._account_space_scope(account_id, space_id),
        }
        return self._request("POST", "api/service/assistant/audit", payload)

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
        structured = {"channel": channel, "sender": sender, **dict(metadata or {})}
        if external_id:
            structured["external_id"] = external_id
        return self.intake(
            text,
            title=title or f"{channel} message from {sender}",
            source="communication",
            source_ref=external_id,
            record_type="message",
            metadata=structured,
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

    def _account_space_scope(self, account_id: str, space_id: str) -> dict[str, str]:
        scope = {
            "account_id": account_id or self.account_id,
            "space_id": space_id or self.space_id,
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


class BrainClient(OneBrainClient):
    """Assistant-facing OneBrain client alias."""
