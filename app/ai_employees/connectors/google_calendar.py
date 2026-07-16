"""Narrow Google Workspace Calendar OAuth and event-write adapter."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, replace
from urllib.parse import quote, urlencode
from uuid import uuid4

from app.ai_employees.base import AiConnectorBinding
from app.ai_employees.connectors.base import (
    ConnectorRequestError,
    ConnectorUnavailableError,
    UrllibJsonTransport,
)
from app.ai_employees.connectors.secrets import assert_opaque_credential_reference
from app.ai_employees.contracts import assert_no_raw_secrets, build_payload_hash, get_ai_employee


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_CALENDAR_SCOPES = (
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
)
GOOGLE_CALENDAR_CAPABILITIES = frozenset({
    "calendar_read",
    "calendar_create_event",
    "calendar_update_event",
    "calendar_cancel_event",
    "calendar_create_private_focus",
})
_EVENT_FIELDS = frozenset({
    "summary", "description", "location", "start", "end", "attendees", "visibility",
    "transparency", "reminders", "colorId", "recurrence",
})


@dataclass(frozen=True)
class OAuthStart:
    authorization_url: str
    state_expires_at: int


class GoogleCalendarConnector:
    target_system = "google_calendar"

    def __init__(
        self,
        *,
        store,
        secret_store,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        state_signing_key: str,
        transport=None,
        timeout_seconds: float = 10.0,
        environment: str = "local",
    ):
        self.store = store
        self.secret_store = secret_store
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.redirect_uri = (redirect_uri or "").strip()
        self.state_signing_key = (state_signing_key or "").encode("utf-8")
        self.transport = transport or UrllibJsonTransport()
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 60.0))
        self.environment = environment

    @property
    def available(self) -> bool:
        return bool(
            self.secret_store and self.client_id and self.client_secret
            and self.redirect_uri and self.state_signing_key
        )

    @property
    def unavailable_reason(self) -> str:
        return "" if self.available else "Google Calendar OAuth is not configured."

    def health(self) -> dict:
        return {
            "provider": self.target_system,
            "available": self.available,
            "reason": self.unavailable_reason,
            "scopes": list(GOOGLE_CALENDAR_SCOPES),
        }

    def start_oauth(
        self,
        *,
        principal,
        account_id: str,
        space_id: str,
        employee_ids: tuple[str, ...],
        capabilities: tuple[str, ...],
        resource_ids: tuple[str, ...] = ("primary",),
    ) -> OAuthStart:
        self._require_available()
        _validate_admin_scope(principal, account_id, space_id)
        employee_ids, capabilities, resource_ids = _validate_grants(
            employee_ids, capabilities, resource_ids,
        )
        verifier = _b64url(secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        nonce = secrets.token_urlsafe(24)
        issued_at = int(time.time())
        pending = {
            "nonce": nonce,
            "issued_at": issued_at,
            "tenant_id": principal.tenant_id,
            "account_id": account_id,
            "space_id": space_id,
            "actor_id": principal.user_id,
            "redirect_uri": self.redirect_uri,
            "code_verifier": verifier,
            "employee_ids": list(employee_ids),
            "capabilities": list(capabilities),
            "resource_ids": list(resource_ids),
        }
        pending_ref = self.secret_store.put(
            provider="google-calendar-oauth-state", account_id=account_id, value=pending,
        )
        state = self._sign_state({
            "nonce": nonce,
            "issued_at": issued_at,
            "pending_ref": pending_ref,
            "account_id": account_id,
            "space_id": space_id,
            "actor_id": principal.user_id,
        })
        query = urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(GOOGLE_CALENDAR_SCOPES),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        return OAuthStart(
            authorization_url=f"{GOOGLE_AUTH_URL}?{query}",
            state_expires_at=issued_at + 600,
        )

    def complete_oauth(
        self,
        *,
        principal,
        state: str,
        code: str,
        expected_account_id: str = "",
        expected_space_id: str = "",
    ) -> AiConnectorBinding:
        self._require_available()
        signed = self._verify_state(state)
        if int(time.time()) - int(signed.get("issued_at", 0)) > 600:
            raise PermissionError("Google Calendar OAuth state expired.")
        _validate_admin_scope(principal, signed.get("account_id", ""), signed.get("space_id", ""))
        if (
            (expected_account_id and signed.get("account_id") != expected_account_id)
            or (expected_space_id and signed.get("space_id") != expected_space_id)
        ):
            raise PermissionError("Google Calendar OAuth scope does not match its initiating request.")
        if signed.get("actor_id") != principal.user_id:
            raise PermissionError("Google Calendar OAuth must finish in the initiating admin session.")
        pending_ref = str(signed.get("pending_ref") or "")
        try:
            pending = self.secret_store.get(pending_ref)
        except Exception as exc:
            raise PermissionError("Google Calendar OAuth state is invalid or already used.") from exc
        if any(pending.get(key) != signed.get(key) for key in ("nonce", "account_id", "space_id", "actor_id")):
            raise PermissionError("Google Calendar OAuth state does not match its initiating request.")
        if pending.get("redirect_uri") != self.redirect_uri:
            raise PermissionError("Google Calendar OAuth redirect URI changed.")
        self.secret_store.delete(pending_ref)
        result = self._form_request(GOOGLE_TOKEN_URL, {
            "code": (code or "").strip(),
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": pending["code_verifier"],
        })
        access_token = str(result.get("access_token") or "")
        refresh_token = str(result.get("refresh_token") or "")
        if not access_token or not refresh_token:
            raise ConnectorRequestError("Google Calendar did not return an offline credential.")
        returned_scopes = frozenset(str(result.get("scope") or "").split())
        if returned_scopes and not set(GOOGLE_CALENDAR_SCOPES).issubset(returned_scopes):
            raise PermissionError("Google Calendar did not grant all required scopes.")
        credential_ref = self.secret_store.put(
            provider="google-calendar",
            account_id=signed["account_id"],
            value={
                "account_id": signed["account_id"],
                "space_id": signed["space_id"],
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": str(result.get("token_type") or "Bearer"),
                "scope": sorted(returned_scopes or GOOGLE_CALENDAR_SCOPES),
                "expires_at_epoch": int(time.time()) + int(result.get("expires_in") or 3600),
            },
        )
        binding = AiConnectorBinding(
            id=f"aicb_{uuid4().hex}",
            tenant_id=principal.tenant_id,
            account_id=signed["account_id"],
            space_id=signed["space_id"],
            provider=self.target_system,
            credential_ref=credential_ref,
            resource_type="calendar",
            resource_ids=tuple(pending["resource_ids"]),
            employee_ids=tuple(pending["employee_ids"]),
            capabilities=tuple(pending["capabilities"]),
            status="active",
        )
        return self.store.save_connector_binding(binding)

    def list_calendars(self, binding: AiConnectorBinding) -> list[dict]:
        self._validate_binding(binding, capability="calendar_read")
        token = self._access_token(binding)
        rows = []
        page_token = ""
        for _ in range(20):
            query = {"maxResults": "250", "showHidden": "false"}
            if page_token:
                query["pageToken"] = page_token
            result = self._api(
                "GET", f"{GOOGLE_CALENDAR_API}/users/me/calendarList?{urlencode(query)}", token,
            )
            for item in result.get("items") or []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                rows.append({
                    "id": str(item["id"]),
                    "summary": str(item.get("summary") or item["id"]),
                    "primary": bool(item.get("primary")),
                    "access_role": str(item.get("accessRole") or ""),
                })
            page_token = str(result.get("nextPageToken") or "")
            if not page_token:
                break
        return rows

    def configure_binding(
        self,
        binding: AiConnectorBinding,
        *,
        employee_ids: tuple[str, ...],
        capabilities: tuple[str, ...],
        resource_ids: tuple[str, ...],
    ) -> AiConnectorBinding:
        self._validate_binding(binding)
        employee_ids, capabilities, resource_ids = _validate_grants(
            employee_ids, capabilities, resource_ids,
        )
        return self.store.save_connector_binding(replace(
            binding,
            employee_ids=employee_ids,
            capabilities=capabilities,
            resource_ids=resource_ids,
        ))

    def revoke(self, binding: AiConnectorBinding) -> AiConnectorBinding:
        self._validate_binding(binding)
        try:
            credential = self.secret_store.get(binding.credential_ref)
            token = credential.get("refresh_token") or credential.get("access_token")
            if token:
                self._form_request(GOOGLE_REVOKE_URL, {"token": token})
        except Exception:
            pass
        self.secret_store.delete(binding.credential_ref)
        return self.store.save_connector_binding(replace(binding, status="revoked"))

    def purge_local_credentials(
        self,
        *,
        account_id: str,
        space_id: str = "",
        bindings: tuple[AiConnectorBinding, ...] = (),
    ) -> int:
        """Erase local connector secrets without depending on Google availability."""
        if not self.secret_store:
            return 0
        references = tuple(binding.credential_ref for binding in bindings)
        return self.secret_store.delete_scope(
            account_id=account_id,
            space_id=space_id,
            references=references,
        )

    def execute(self, proposal, binding) -> str:
        self._validate_binding(binding, capability=proposal.action_type, employee_id=proposal.employee_id)
        if build_payload_hash(proposal.payload) != proposal.payload_hash:
            raise PermissionError("Approved Google Calendar payload no longer matches.")
        calendar_id = str(proposal.payload.get("calendar_id") or "")
        if not calendar_id or calendar_id not in binding.resource_ids:
            raise PermissionError("Google Calendar is not in the binding allowlist.")
        token = self._access_token(binding)
        if proposal.action_type == "calendar_create_event":
            return self._create_event(proposal, calendar_id, token)
        if proposal.action_type == "calendar_update_event":
            return self._update_event(proposal, calendar_id, token)
        if proposal.action_type == "calendar_cancel_event":
            return self._cancel_event(proposal, calendar_id, token)
        raise PermissionError("Unsupported Google Calendar action type.")

    def _create_event(self, proposal, calendar_id: str, token: str) -> str:
        event = _normalized_event(proposal.payload.get("event"))
        event_id = "ob" + hashlib.sha256(proposal.idempotency_key.encode("utf-8")).hexdigest()[:30]
        event["id"] = event_id
        event["extendedProperties"] = {"private": {
            "onebrain_action_id": proposal.id,
            "onebrain_payload_hash": proposal.payload_hash,
            "onebrain_idempotency_key": proposal.idempotency_key[:100],
        }}
        has_attendees = bool(event.get("attendees"))
        query = urlencode({"sendUpdates": "all" if has_attendees else "none"})
        try:
            result = self._api(
                "POST",
                f"{GOOGLE_CALENDAR_API}/calendars/{quote(calendar_id, safe='')}/events?{query}",
                token,
                payload=event,
            )
        except ConnectorRequestError as exc:
            if exc.status_code != 409:
                raise
            result = self._api(
                "GET",
                f"{GOOGLE_CALENDAR_API}/calendars/{quote(calendar_id, safe='')}/events/{event_id}",
                token,
            )
        returned_id = str(result.get("id") or "")
        if returned_id != event_id:
            raise ConnectorRequestError("Google Calendar returned an unexpected event reference.")
        return returned_id

    def _update_event(self, proposal, calendar_id: str, token: str) -> str:
        event_id = _required_payload_string(proposal.payload, "event_id")
        etag = _required_payload_string(proposal.payload, "etag")
        event = _normalized_event(proposal.payload.get("event"), partial=True)
        has_attendees = bool(event.get("attendees"))
        result = self._api(
            "PATCH",
            f"{GOOGLE_CALENDAR_API}/calendars/{quote(calendar_id, safe='')}/events/"
            f"{quote(event_id, safe='')}?{urlencode({'sendUpdates': 'all' if has_attendees else 'none'})}",
            token,
            payload=event,
            extra_headers={"If-Match": etag},
        )
        if str(result.get("id") or "") != event_id:
            raise ConnectorRequestError("Google Calendar returned an unexpected event reference.")
        return event_id

    def _cancel_event(self, proposal, calendar_id: str, token: str) -> str:
        event_id = _required_payload_string(proposal.payload, "event_id")
        etag = _required_payload_string(proposal.payload, "etag")
        self._api(
            "DELETE",
            f"{GOOGLE_CALENDAR_API}/calendars/{quote(calendar_id, safe='')}/events/"
            f"{quote(event_id, safe='')}?sendUpdates=all",
            token,
            extra_headers={"If-Match": etag},
        )
        return event_id

    def _access_token(self, binding: AiConnectorBinding) -> str:
        try:
            credential = self.secret_store.get(binding.credential_ref)
        except Exception as exc:
            raise ConnectorUnavailableError("Google Calendar credential is unavailable.") from exc
        if int(credential.get("expires_at_epoch") or 0) > int(time.time()) + 60:
            token = str(credential.get("access_token") or "")
            if token:
                return token
        refresh_token = str(credential.get("refresh_token") or "")
        if not refresh_token:
            raise ConnectorUnavailableError("Google Calendar offline credential is unavailable.")
        result = self._form_request(GOOGLE_TOKEN_URL, {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
        access_token = str(result.get("access_token") or "")
        if not access_token:
            raise ConnectorRequestError("Google Calendar token refresh failed.")
        credential.update({
            "access_token": access_token,
            "expires_at_epoch": int(time.time()) + int(result.get("expires_in") or 3600),
        })
        self.secret_store.update(binding.credential_ref, credential)
        return access_token

    def _form_request(self, url: str, values: dict) -> dict:
        body = urlencode(values).encode("utf-8")
        result = self.transport.request(
            "POST",
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body,
            timeout=self.timeout_seconds,
        )
        if not 200 <= result.status < 300:
            raise ConnectorRequestError(
                f"External connector request failed with HTTP {result.status}.",
                status_code=result.status,
            )
        return result.payload

    def _api(self, method, url, token, *, payload=None, extra_headers=None) -> dict:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        body = b""
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers.update(extra_headers or {})
        result = self.transport.request(
            method, url, headers=headers, body=body, timeout=self.timeout_seconds,
        )
        if not 200 <= result.status < 300:
            raise ConnectorRequestError(
                f"External connector request failed with HTTP {result.status}.",
                status_code=result.status,
            )
        return result.payload

    def _sign_state(self, payload: dict) -> str:
        encoded = _b64url(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        signature = _b64url(hmac.new(self.state_signing_key, encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def _verify_state(self, state: str) -> dict:
        try:
            encoded, supplied = (state or "").split(".", 1)
            expected = _b64url(hmac.new(
                self.state_signing_key, encoded.encode("ascii"), hashlib.sha256,
            ).digest())
            if not hmac.compare_digest(supplied, expected):
                raise ValueError
            payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PermissionError("Google Calendar OAuth state is invalid.") from exc
        if not isinstance(payload, dict):
            raise PermissionError("Google Calendar OAuth state is invalid.")
        return payload

    def _validate_binding(self, binding, *, capability="", employee_id="") -> None:
        assert_opaque_credential_reference(binding.credential_ref)
        if binding.provider != self.target_system or binding.status != "active":
            raise ConnectorUnavailableError("Google Calendar binding is not active.")
        if capability and capability not in binding.capabilities:
            raise PermissionError("Google Calendar capability is not granted.")
        if employee_id and employee_id not in binding.employee_ids:
            raise PermissionError("AI employee is not assigned to this Google Calendar binding.")

    def _require_available(self) -> None:
        if not self.available:
            raise ConnectorUnavailableError(self.unavailable_reason)
        if self.environment in {"production", "staging"} and not self.redirect_uri.startswith("https://"):
            raise ConnectorUnavailableError("Google Calendar OAuth requires an HTTPS redirect URI.")


def _validate_admin_scope(principal, account_id: str, space_id: str) -> None:
    if (
        principal.principal_type != "human"
        or principal.role_id != "admin"
        or principal.tenant_id != account_id
        or (principal.account_id and principal.account_id != account_id)
        or (principal.space_ids is not None and space_id not in principal.space_ids)
    ):
        raise PermissionError("A scoped human account admin must manage Google Calendar OAuth.")


def _validate_grants(employee_ids, capabilities, resource_ids):
    employees = tuple(dict.fromkeys((value or "").strip() for value in employee_ids if (value or "").strip()))
    capabilities = tuple(dict.fromkeys((value or "").strip() for value in capabilities if (value or "").strip()))
    resources = tuple(dict.fromkeys((value or "").strip() for value in resource_ids if (value or "").strip()))
    if not employees or not capabilities or not resources:
        raise ValueError("Google Calendar requires employees, capabilities, and allowed calendars.")
    for employee_id in employees:
        get_ai_employee(employee_id)
    unknown = set(capabilities) - GOOGLE_CALENDAR_CAPABILITIES
    if unknown:
        raise ValueError(f"Unknown Google Calendar capability: {sorted(unknown)[0]}")
    return employees, capabilities, resources


def _normalized_event(value, *, partial: bool = False) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Google Calendar event must be an object.")
    unknown = set(value) - _EVENT_FIELDS
    if unknown:
        raise ValueError(f"Unsupported Google Calendar event field: {sorted(unknown)[0]}")
    event = dict(value)
    assert_no_raw_secrets(event, "google_calendar.event")
    if not partial:
        for required in ("summary", "start", "end"):
            if not event.get(required):
                raise ValueError(f"Google Calendar event requires {required}.")
    if "attendees" in event:
        attendees = event["attendees"]
        if not isinstance(attendees, list) or any(
            not isinstance(row, dict) or set(row) - {"email", "displayName"} or not row.get("email")
            for row in attendees
        ):
            raise ValueError("Google Calendar attendees must contain only email and displayName.")
    for field in ("start", "end"):
        if field not in event:
            continue
        value = event[field]
        if not isinstance(value, dict) or set(value) - {"date", "dateTime", "timeZone"}:
            raise ValueError(f"Google Calendar {field} is malformed.")
        if bool(value.get("date")) == bool(value.get("dateTime")):
            raise ValueError(f"Google Calendar {field} requires exactly one of date or dateTime.")
    if event.get("visibility") not in {None, "default", "public", "private", "confidential"}:
        raise ValueError("Unsupported Google Calendar visibility.")
    return event


def is_private_self_only_focus_payload(payload: dict) -> bool:
    if not isinstance(payload, dict) or payload.get("automation_kind") != "private_focus":
        return False
    if payload.get("self_only") is not True:
        return False
    event = payload.get("event")
    if not isinstance(event, dict) or event.get("attendees"):
        return False
    if event.get("visibility") != "private":
        return False
    try:
        _normalized_event(event)
    except ValueError:
        return False
    return True


def _required_payload_string(payload: dict, field: str) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise ValueError(f"Google Calendar action requires {field}.")
    return value


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
