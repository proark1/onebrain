"""Google Calendar OAuth, grant, token, and idempotent-write contracts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet

from app.ai_employees.actions import ActionExecutorRegistry, AiEmployeeActionService
from app.ai_employees.connectors.base import HttpResult
from app.ai_employees.connectors.google_calendar import (
    GOOGLE_CALENDAR_SCOPES,
    GoogleCalendarConnector,
)
from app.ai_employees.connectors.secrets import (
    EncryptedFileConnectorSecretStore,
    MemoryConnectorSecretStore,
)
from app.ai_employees.memory import MemoryAiEmployeeStore
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.intake.base import IntakeRecord
from app.intake.memory import MemoryIntakeStore


SCOPE = {"tenant_id": "acme", "account_id": "acme", "space_id": "business"}


class Transport:
    def __init__(self):
        self.calls = []
        self.responses = []

    def queue(self, status=200, payload=None, headers=None):
        self.responses.append(HttpResult(status, payload or {}, headers or {}))

    def request(self, method, url, *, headers=None, body=b"", timeout=10.0):
        self.calls.append({
            "method": method, "url": url, "headers": dict(headers or {}),
            "body": body, "timeout": timeout,
        })
        if not self.responses:
            raise AssertionError(f"Unexpected request: {method} {url}")
        return self.responses.pop(0)


def _principal():
    role = ROLES["admin"]
    return Principal(
        user_id="admin@acme", role_id=role.id, role_label=role.label,
        clearance=role.clearance, locations=None, categories=None, location_label="all",
        tenant_id="acme", account_id="acme", space_ids=frozenset({"business"}),
        session_id="fresh",
    )


def _connector(*, secret_store=None, transport=None):
    employees = MemoryAiEmployeeStore()
    employees.seed_defaults(**SCOPE, author_id="system:test")
    transport = transport or Transport()
    secret_store = secret_store or MemoryConnectorSecretStore()
    connector = GoogleCalendarConnector(
        store=employees,
        secret_store=secret_store,
        client_id="google-client-id",
        client_secret="google-client-secret",
        redirect_uri="https://onebrain.example/oauth/google-calendar/callback",
        state_signing_key="state-signing-key",
        transport=transport,
        environment="production",
    )
    return connector, employees, secret_store, transport


def _connect(connector, transport):
    start = connector.start_oauth(
        principal=_principal(), account_id="acme", space_id="business",
        employee_ids=("chief_of_staff",),
        capabilities=(
            "calendar_read", "calendar_create_event", "calendar_create_private_focus",
        ),
        resource_ids=("primary",),
    )
    state = parse_qs(urlparse(start.authorization_url).query)["state"][0]
    transport.queue(payload={
        "access_token": "access-token-value",
        "refresh_token": "refresh-token-value",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": " ".join(GOOGLE_CALENDAR_SCOPES),
    })
    binding = connector.complete_oauth(
        principal=_principal(), state=state, code="one-time-code",
    )
    return start, state, binding


def test_oauth_state_is_signed_one_time_pkce_bound_and_tokens_stay_opaque():
    connector, employees, secrets_store, transport = _connector()
    start, state, binding = _connect(connector, transport)
    query = parse_qs(urlparse(start.authorization_url).query)
    assert query["access_type"] == ["offline"]
    assert query["code_challenge_method"] == ["S256"]
    assert set(query["scope"][0].split()) == set(GOOGLE_CALENDAR_SCOPES)
    assert binding.credential_ref.startswith("secret://ai-employees/google-calendar/")
    assert "access-token-value" not in json.dumps(employees.export_scope(**SCOPE))
    credential = secrets_store.get(binding.credential_ref)
    assert credential["refresh_token"] == "refresh-token-value"
    assert credential["space_id"] == "business"
    token_exchange = transport.calls[-1]
    assert token_exchange["url"].endswith("/token")
    assert b"code_verifier=" in token_exchange["body"]

    with pytest.raises(PermissionError, match="already used"):
        connector.complete_oauth(principal=_principal(), state=state, code="replay")
    damaged = state[:-1] + ("a" if state[-1] != "a" else "b")
    with pytest.raises(PermissionError, match="state is invalid"):
        connector.complete_oauth(principal=_principal(), state=damaged, code="bad")


def test_scope_purge_removes_credentials_and_pending_oauth_without_network():
    connector, _, secret_store, transport = _connector()
    _, _, binding = _connect(connector, transport)
    pending_ref = secret_store.put(
        provider="google-calendar-oauth-state",
        account_id="acme",
        value={"account_id": "acme", "space_id": "business"},
    )
    other_space_ref = secret_store.put(
        provider="google-calendar-oauth-state",
        account_id="acme",
        value={"account_id": "acme", "space_id": "personal"},
    )

    deleted = connector.purge_local_credentials(
        account_id="acme",
        space_id="business",
        bindings=(binding,),
    )

    assert deleted == 2
    with pytest.raises(KeyError):
        secret_store.get(binding.credential_ref)
    with pytest.raises(KeyError):
        secret_store.get(pending_ref)
    assert secret_store.get(other_space_ref)["space_id"] == "personal"
    assert len(transport.calls) == 1


def test_calendar_list_and_binding_grants_are_narrow_and_provider_tokens_refresh():
    connector, _, secret_store, transport = _connector()
    _, _, binding = _connect(connector, transport)
    credential = secret_store.get(binding.credential_ref)
    credential["expires_at_epoch"] = 1
    secret_store.update(binding.credential_ref, credential)
    transport.queue(payload={"access_token": "refreshed-access", "expires_in": 3600})
    transport.queue(payload={"items": [
        {"id": "primary", "summary": "Main", "primary": True, "accessRole": "owner"},
        {"id": "team", "summary": "Team", "accessRole": "writer"},
    ]})
    calendars = connector.list_calendars(binding)
    assert [row["id"] for row in calendars] == ["primary", "team"]
    assert transport.calls[-1]["headers"]["Authorization"] == "Bearer refreshed-access"
    assert secret_store.get(binding.credential_ref)["access_token"] == "refreshed-access"

    with pytest.raises(ValueError, match="Unknown Google Calendar capability"):
        connector.configure_binding(
            binding,
            employee_ids=("chief_of_staff",), capabilities=("calendar_delete_all",),
            resource_ids=("primary",),
        )


def test_private_self_only_focus_policy_can_execute_once_without_second_approval():
    connector, employees, _, transport = _connector()
    _, _, binding = _connect(connector, transport)
    intake = MemoryIntakeStore()
    intake.create(IntakeRecord(
        id="source-1", **SCOPE, app_id="core", purpose="knowledge", source="upload",
        source_ref="source-1", record_type="document", intent="knowledge_update",
        classification="internal", confidence=1.0, status="approved", title="Plan",
        content="Approved focus plan", summary="Focus plan", metadata={"category": "general"},
    ))
    service = AiEmployeeActionService(
        store=employees,
        intake_store=intake,
        executor_registry=ActionExecutorRegistry([connector]),
    )
    proposal = service.propose(
        principal=_principal(), employee_id="chief_of_staff",
        action_type="calendar_create_event", target_system="google_calendar",
        risk_level="low", classification="internal", actionability="automation_allowed",
        source_record_ids=("source-1",), payload_summary="Protect private focus time",
        payload={
            "calendar_id": "primary",
            "automation_kind": "private_focus",
            "self_only": True,
            "event": {
                "summary": "Focus time", "visibility": "private",
                "start": {"dateTime": "2026-07-20T09:00:00+02:00"},
                "end": {"dateTime": "2026-07-20T10:00:00+02:00"},
            },
        },
        idempotency_key="private-focus-2026-07-20",
    )
    assert proposal.status == "approved"
    assert proposal.requires_approval is False
    assert proposal.approved_by == "policy:private_self_only_focus"
    transport.queue(payload={"id": "ob" + __import__("hashlib").sha256(
        proposal.idempotency_key.encode(),
    ).hexdigest()[:30]})
    executed = service.execute(principal=_principal(), proposal_id=proposal.id)
    assert executed.status == "executed"
    assert service.execute(principal=_principal(), proposal_id=proposal.id).execution_ref == executed.execution_ref
    event_write = transport.calls[-1]
    body = json.loads(event_write["body"])
    assert body["visibility"] == "private"
    assert body["extendedProperties"]["private"]["onebrain_payload_hash"] == proposal.payload_hash
    assert "sendUpdates=none" in event_write["url"]


def test_duplicate_provider_event_is_read_by_deterministic_id_not_created_blindly():
    connector, employees, _, transport = _connector()
    _, _, binding = _connect(connector, transport)
    from app.ai_employees.base import AiActionProposalRecord
    from app.ai_employees.contracts import build_payload_hash

    payload = {
        "calendar_id": "primary",
        "event": {
            "summary": "Launch review",
            "start": {"dateTime": "2026-07-20T11:00:00+02:00"},
            "end": {"dateTime": "2026-07-20T12:00:00+02:00"},
        },
    }
    proposal = AiActionProposalRecord(
        id="action-1", **SCOPE, mission_id="", conversation_id="", run_id="",
        employee_id="chief_of_staff", action_type="calendar_create_event",
        target_system="google_calendar", risk_level="medium", classification="internal",
        actionability="approval_required", source_record_ids=("source-1",),
        payload_summary="Launch review", payload=payload, payload_hash=build_payload_hash(payload),
        required_approver_role="account_admin", expires_at=(
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat(), idempotency_key="same-event", status="approved", requires_approval=True,
        reason="Approved", approved_by="admin@acme", approved_at=datetime.now(timezone.utc).isoformat(),
    )
    event_id = "ob" + __import__("hashlib").sha256(b"same-event").hexdigest()[:30]
    transport.queue(status=409, payload={"error": {"code": 409}})
    transport.queue(payload={"id": event_id})
    assert connector.execute(proposal, binding) == event_id
    assert [row["method"] for row in transport.calls[-2:]] == ["POST", "GET"]


def test_encrypted_secret_backend_never_writes_raw_tokens(tmp_path):
    path = tmp_path / "connector-secrets.json"
    store = EncryptedFileConnectorSecretStore(
        path=str(path), encryption_key=Fernet.generate_key().decode("ascii"),
    )
    ref = store.put(
        provider="google-calendar", account_id="acme",
        value={
            "account_id": "acme",
            "space_id": "business",
            "access_token": "very-raw-access-token",
            "refresh_token": "very-raw-refresh-token",
        },
    )
    other_ref = store.put(
        provider="google-calendar", account_id="acme",
        value={"account_id": "acme", "space_id": "personal", "access_token": "other"},
    )
    raw = path.read_text(encoding="utf-8")
    assert "very-raw-access-token" not in raw
    assert "very-raw-refresh-token" not in raw
    assert store.get(ref)["refresh_token"] == "very-raw-refresh-token"
    assert store.delete_scope(account_id="acme", space_id="business") == 1
    with pytest.raises(KeyError):
        store.get(ref)
    assert store.get(other_ref)["access_token"] == "other"


def test_revoke_marks_the_binding_revoked_when_google_accepts_it():
    connector, _, secrets_store, transport = _connector()
    _, _, binding = _connect(connector, transport)
    transport.queue(status=200, payload={})

    revoked = connector.revoke(binding)

    assert revoked.status == "revoked"
    assert transport.calls[-1]["url"].endswith("/revoke")
    with pytest.raises(KeyError):
        secrets_store.get(binding.credential_ref)


def test_revoke_does_not_claim_success_when_google_rejects_it():
    """The audit trail must not record a revocation that did not happen.

    The local credential is erased either way, so the upstream call can never be
    retried -- the offline grant stays live at Google until the user removes it
    from their own account. Reporting "revoked" here would tell an offboarding
    audit the access was withdrawn when it was not.
    """
    connector, _, secrets_store, transport = _connector()
    _, _, binding = _connect(connector, transport)
    transport.queue(status=503, payload={"error": "backend_error"})

    result = connector.revoke(binding)

    assert result.status == "error"
    # Still erased locally: OneBrain does not retain a credential it was told to
    # revoke, even when it could not withdraw it upstream.
    with pytest.raises(KeyError):
        secrets_store.get(binding.credential_ref)


def test_revoke_of_an_already_erased_credential_is_an_honest_success():
    connector, _, secrets_store, transport = _connector()
    _, _, binding = _connect(connector, transport)
    secrets_store.delete(binding.credential_ref)

    result = connector.revoke(binding)

    # Nothing of ours is left to withdraw, so there is no upstream call to fail.
    assert result.status == "revoked"
