"""Provenance-bound, human-approved memory workflows."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from app.ai_employees.base import AiEmployeeMemory
from app.ai_employees.contracts import get_ai_employee
from app.security.policy import Classification


def create_memory_candidate(
    store,
    intake_store,
    *,
    principal,
    tenant_id: str,
    account_id: str,
    space_id: str,
    employee_id: str,
    content: str,
    source_refs: tuple[str, ...],
    classification: str,
    retention_until: str,
    author_id: str,
) -> AiEmployeeMemory:
    get_ai_employee(employee_id)
    content = (content or "").strip()
    if not 1 <= len(content) <= 4_000:
        raise ValueError("AI employee memory content must contain 1 to 4000 characters.")
    source_refs = tuple(dict.fromkeys((source_ref or "").strip() for source_ref in source_refs))
    if not source_refs or any(not source_ref for source_ref in source_refs):
        raise ValueError("AI employee memory requires source provenance.")
    requested_classification = Classification.parse(classification)
    for source_ref in source_refs:
        record = intake_store.get(
            source_ref,
            tenant_id=tenant_id,
            account_id=account_id,
            space_id=space_id,
        )
        # Anything the caller may not read has to fail identically, or this
        # endpoint is a classification oracle: the scope check alone is space
        # membership, so a holder of ai_employee_mission_run could otherwise
        # binary-search the classification of any intake id in the space --
        # including HR and finance records far above their clearance -- from
        # which of the two distinct errors came back. `_accessible_sources` in
        # actions.py collapses the same cases for the same reason.
        unavailable = PermissionError(f"AI employee memory source is unavailable: {source_ref}")
        if not record or record.status != "approved":
            raise unavailable
        source_classification = Classification.parse(record.classification)
        if source_classification > principal.clearance:
            raise unavailable
        category = record.metadata.get("category", "general")
        if principal.categories is not None and category != "general" and category not in principal.categories:
            raise unavailable
        # Past this point the caller demonstrably may read the record, so naming
        # the reason leaks nothing they do not already hold.
        if requested_classification < source_classification:
            raise ValueError("AI employee memory cannot lower its source classification.")
    retention = _parse_future_timestamp(retention_until, "retention_until")
    return store.save_memory(AiEmployeeMemory(
        id=f"aimem_{uuid4().hex}",
        tenant_id=tenant_id,
        account_id=account_id,
        space_id=space_id,
        employee_id=employee_id,
        content=content,
        source_refs=source_refs,
        classification=requested_classification.name.lower(),
        status="pending",
        retention_until=retention,
        author_id=author_id,
    ))


def decide_memory(
    store,
    memory_id: str,
    *,
    tenant_id: str,
    account_id: str,
    space_id: str,
    decision: str,
    actor_id: str,
) -> AiEmployeeMemory:
    memory = store.get_memory(
        memory_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
    )
    if not memory:
        raise KeyError(f"AI employee memory not found: {memory_id}")
    if memory.status != "pending":
        raise ValueError("Only pending AI employee memory can be approved or rejected.")
    if decision not in {"approved", "rejected"}:
        raise ValueError("AI employee memory decision must be approved or rejected.")
    return store.save_memory(replace(
        memory,
        status=decision,
        approved_by=actor_id if decision == "approved" else "",
        approved_at=datetime.now(timezone.utc).isoformat() if decision == "approved" else "",
    ))


def delete_memory(
    store,
    memory_id: str,
    *,
    tenant_id: str,
    account_id: str,
    space_id: str,
) -> AiEmployeeMemory:
    memory = store.get_memory(
        memory_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
    )
    if not memory:
        raise KeyError(f"AI employee memory not found: {memory_id}")
    return store.save_memory(replace(memory, status="deleted"))


def active_approved_memories(memories: list[AiEmployeeMemory], *, now: datetime | None = None):
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return [
        memory for memory in memories
        if memory.status == "approved"
        and _parse_timestamp(memory.retention_until, "retention_until") > current
    ]


def _parse_future_timestamp(value: str, field: str) -> str:
    parsed = _parse_timestamp(value, field)
    if parsed <= datetime.now(timezone.utc):
        raise ValueError(f"{field} must be in the future.")
    return parsed.isoformat()


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone.")
    return parsed.astimezone(timezone.utc)
