"""Governed AI employee work-product, approval, and execution contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.ai_employees.actions import ActionExecutorRegistry, AiEmployeeActionService
from app.ai_employees.base import AiConnectorBinding
from app.ai_employees.memory import MemoryAiEmployeeStore
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.intake.base import IntakeRecord
from app.intake.memory import MemoryIntakeStore
from app.sessions.base import Session
from app.sessions.memory import MemorySessionStore


SCOPE = {"tenant_id": "acme", "account_id": "acme", "space_id": "business"}


class CalendarExecutor:
    target_system = "google_calendar"

    def __init__(self):
        self.calls = []

    def execute(self, proposal, binding):
        self.calls.append((proposal.id, binding.id, proposal.payload_hash))
        return "google-event-123"


def _principal(*, role_id="admin", session_id="session-1"):
    role = ROLES[role_id]
    return Principal(
        user_id="admin@acme",
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=None,
        categories=role.categories,
        location_label="all",
        tenant_id="acme",
        account_id="acme",
        space_ids=frozenset({"business"}),
        session_id=session_id,
    )


def _source(intake, *, record_id="source-1", classification="internal", status="approved"):
    return intake.create(IntakeRecord(
        id=record_id,
        **SCOPE,
        app_id="core",
        purpose="knowledge",
        source="upload",
        source_ref=record_id,
        record_type="document",
        intent="knowledge_update",
        classification=classification,
        confidence=1.0,
        status=status,
        title="Source",
        content="Approved operating context.",
        summary="Approved context.",
        metadata={"category": "general"},
    ))


def _service(*, session_created_at=None):
    employees = MemoryAiEmployeeStore()
    employees.seed_defaults(**SCOPE, author_id="system:test")
    intake = MemoryIntakeStore()
    sessions = MemorySessionStore()
    sessions.create(Session(
        id="session-1",
        user_id="admin@acme",
        tenant_id="acme",
        created_at=session_created_at or datetime.now(timezone.utc).isoformat(),
        expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    ))
    executor = CalendarExecutor()
    service = AiEmployeeActionService(
        store=employees,
        intake_store=intake,
        session_store=sessions,
        executor_registry=ActionExecutorRegistry([executor]),
    )
    return service, employees, intake, executor


def _calendar_proposal(service, intake, **changes):
    _source(intake)
    values = {
        "principal": _principal(),
        "employee_id": "chief_of_staff",
        "action_type": "calendar_create_event",
        "target_system": "google_calendar",
        "risk_level": "medium",
        "classification": "internal",
        "actionability": "approval_required",
        "source_record_ids": ("source-1",),
        "payload_summary": "Create the launch planning meeting.",
        "payload": {"calendar_id": "primary", "summary": "Launch planning"},
        "idempotency_key": "calendar-launch-1",
    }
    values.update(changes)
    return service.propose(**values)


def test_internal_work_products_are_source_bound_and_create_auditable_intake_records():
    service, _, intake, _ = _service()
    _source(intake, classification="confidential")
    result = service.create_work_product(
        principal=_principal(),
        employee_id="finance_manager",
        record_type="brief",
        title="Cash position",
        content="Cash remains inside the agreed operating envelope.",
        classification="confidential",
        source_record_ids=("source-1",),
    )
    assert result.record.app_id == "ai_employees"
    assert result.record.status == "approved"
    assert result.record.metadata["generated_by_employee_id"] == "finance_manager"
    assert result.audit.record_type == "action_audit"
    assert intake.count() == 3

    with pytest.raises(ValueError, match="cannot lower"):
        service.create_work_product(
            principal=_principal(), employee_id="finance_manager", record_type="brief",
            title="Unsafe downgrade", content="Text", classification="internal",
            source_record_ids=("source-1",),
        )


def test_action_proposals_are_capability_checked_payload_hashed_and_idempotent():
    service, _, intake, _ = _service()
    proposal = _calendar_proposal(service, intake)
    assert proposal.requires_approval is True
    assert len(proposal.payload_hash) == 64
    assert proposal.required_approver_role == "account_admin"
    replay = service.propose(
        principal=_principal(),
        employee_id="chief_of_staff",
        action_type="calendar_create_event",
        target_system="google_calendar",
        risk_level="medium",
        classification="internal",
        actionability="approval_required",
        source_record_ids=("source-1",),
        payload_summary="Create the launch planning meeting.",
        payload={"calendar_id": "primary", "summary": "Launch planning"},
        idempotency_key="calendar-launch-1",
    )
    assert replay.id == proposal.id

    with pytest.raises(PermissionError, match="not granted"):
        service.propose(
            principal=_principal(), employee_id="finance_manager",
            action_type="publish_social", target_system="social", risk_level="medium",
            classification="internal", actionability="approval_required",
            source_record_ids=("source-1",), payload_summary="Publish", payload={"text": "Hi"},
            idempotency_key="bad-capability",
        )


def test_approval_requires_fresh_human_session_role_and_unchanged_payload():
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    stale_service, _, stale_intake, _ = _service(session_created_at=old)
    stale = _calendar_proposal(stale_service, stale_intake)
    with pytest.raises(PermissionError, match="re-authenticate"):
        stale_service.decide(
            principal=_principal(), proposal_id=stale.id, decision="approved",
        )

    service, employees, intake, _ = _service()
    proposal = _calendar_proposal(service, intake)
    employees.save_action_proposal(replace(proposal, payload={"calendar_id": "other"}))
    with pytest.raises(ValueError, match="payload changed"):
        service.decide(
            principal=_principal(), proposal_id=proposal.id, decision="approved",
        )


def test_execution_rechecks_approval_payload_employee_and_live_connector_grants():
    service, employees, intake, executor = _service()
    proposal = _calendar_proposal(service, intake)
    with pytest.raises(PermissionError, match="valid human approval"):
        service.execute(principal=_principal(), proposal_id=proposal.id)

    approved = service.decide(
        principal=_principal(), proposal_id=proposal.id, decision="approved",
    )
    blocked = service.execute(principal=_principal(), proposal_id=approved.id)
    assert blocked.status == "blocked_by_policy"
    assert executor.calls == []

    # A new proposal receives a current grant and executes exactly once.
    proposal2 = service.propose(
        principal=_principal(), employee_id="chief_of_staff",
        action_type="calendar_create_event", target_system="google_calendar",
        risk_level="medium", classification="internal", actionability="approval_required",
        source_record_ids=("source-1",), payload_summary="Create another meeting",
        payload={"calendar_id": "primary", "summary": "Second meeting"},
        idempotency_key="calendar-launch-2",
    )
    approved2 = service.decide(
        principal=_principal(), proposal_id=proposal2.id, decision="approved",
    )
    employees.save_connector_binding(AiConnectorBinding(
        id="binding-1",
        **SCOPE,
        provider="google_calendar",
        credential_ref="secret://google-calendar/acme",
        resource_type="calendar",
        resource_ids=("primary",),
        employee_ids=("chief_of_staff",),
        capabilities=("calendar_create_event",),
        status="active",
    ))
    executed = service.execute(principal=_principal(), proposal_id=approved2.id)
    assert executed.status == "executed"
    assert executed.execution_ref == "google-event-123"
    assert service.execute(principal=_principal(), proposal_id=approved2.id).execution_ref == "google-event-123"
    assert len(executor.calls) == 1
