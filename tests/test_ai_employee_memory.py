"""Approved-memory provenance and retention contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.ai_employees.memory import MemoryAiEmployeeStore
from app.ai_employees.memory_service import (
    active_approved_memories,
    create_memory_candidate,
    decide_memory,
    delete_memory,
)
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.intake.base import IntakeRecord
from app.intake.memory import MemoryIntakeStore


SCOPE = {"tenant_id": "acme", "account_id": "acme", "space_id": "business"}


def _stores():
    employees = MemoryAiEmployeeStore()
    employees.seed_defaults(**SCOPE, author_id="system:test")
    intake = MemoryIntakeStore()
    intake.create(IntakeRecord(
        id="rec-approved", **SCOPE, app_id="ai_employees", purpose="ai_employee_read",
        source="upload", source_ref="source-1", record_type="fact", intent="knowledge_update",
        classification="internal", confidence=1.0, status="approved", title="Preference",
        content="Board reports use EUR.", summary="EUR reporting.",
    ))
    intake.create(IntakeRecord(
        id="rec-pending", **SCOPE, app_id="ai_employees", purpose="ai_employee_read",
        source="upload", source_ref="source-2", record_type="fact", intent="knowledge_update",
        classification="internal", confidence=1.0, status="pending", title="Pending",
        content="Pending claim.", summary="Pending.",
    ))
    return employees, intake


def _future(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _principal(role_id: str = "admin"):
    role = ROLES[role_id]
    return Principal(
        user_id=f"{role_id}@acme", role_id=role.id, role_label=role.label,
        clearance=role.clearance,
        locations=None if role.scope == "chain" else frozenset({"berlin"}),
        categories=role.categories, location_label="all", tenant_id="acme",
    )


def test_memory_candidate_requires_approved_scoped_provenance_and_cannot_lower_classification():
    store, intake = _stores()
    candidate = create_memory_candidate(
        store, intake, principal=_principal(), **SCOPE, employee_id="finance_manager",
        content="Board reports use EUR.", source_refs=("rec-approved",),
        classification="internal", retention_until=_future(), author_id="user",
    )
    assert candidate.status == "pending"
    assert candidate.source_refs == ("rec-approved",)

    with pytest.raises(PermissionError, match="unavailable"):
        create_memory_candidate(
            store, intake, principal=_principal(), **SCOPE, employee_id="finance_manager",
            content="Pending claim.", source_refs=("rec-pending",),
            classification="internal", retention_until=_future(), author_id="user",
        )
    confidential = replace(intake.get("rec-approved"), id="rec-confidential", classification="confidential")
    intake.create(confidential)
    # The admin is cleared for the record, so naming the reason leaks nothing.
    with pytest.raises(ValueError, match="cannot lower"):
        create_memory_candidate(
            store, intake, principal=_principal(), **SCOPE, employee_id="finance_manager",
            content="Sensitive preference.", source_refs=("rec-confidential",),
            classification="internal", retention_until=_future(), author_id="user",
        )


def test_memory_sources_above_the_caller_clearance_are_indistinguishable_from_absent():
    """Otherwise this endpoint is a classification oracle.

    Its only access gate is space membership plus the mission-run purpose, so a
    front_desk holder could binary-search the classification of any intake id in
    the space -- including HR and finance records far above their clearance --
    purely from which error came back.
    """
    store, intake = _stores()
    intake.create(replace(intake.get("rec-approved"), id="rec-hr", classification="restricted"))
    front_desk = _principal("front_desk")   # INTERNAL clearance

    def attempt(source_ref: str) -> str:
        with pytest.raises(PermissionError) as raised:
            create_memory_candidate(
                store, intake, principal=front_desk, **SCOPE, employee_id="finance_manager",
                content="Probe.", source_refs=(source_ref,),
                classification="restricted", retention_until=_future(), author_id="user",
            )
        return str(raised.value)

    # A record that exists but is above the caller's clearance must read exactly
    # like one that does not exist at all, beyond the id they already supplied.
    assert attempt("rec-hr").replace("rec-hr", "X") == attempt("rec-missing").replace("rec-missing", "X")


def test_memory_requires_human_decision_and_expired_or_deleted_memory_is_not_active():
    store, intake = _stores()
    candidate = create_memory_candidate(
        store, intake, principal=_principal(), **SCOPE, employee_id="finance_manager",
        content="Board reports use EUR.", source_refs=("rec-approved",),
        classification="internal", retention_until=_future(), author_id="user",
    )
    assert active_approved_memories([candidate]) == []
    approved = decide_memory(
        store, candidate.id, **SCOPE, decision="approved", actor_id="admin",
    )
    assert approved.approved_by == "admin"
    assert active_approved_memories([approved]) == [approved]
    with pytest.raises(ValueError, match="Only pending"):
        decide_memory(store, candidate.id, **SCOPE, decision="rejected", actor_id="admin")

    expired = replace(
        approved,
        retention_until=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    assert active_approved_memories([expired]) == []
    deleted = delete_memory(store, candidate.id, **SCOPE)
    assert deleted.status == "deleted"
    assert active_approved_memories([deleted]) == []
