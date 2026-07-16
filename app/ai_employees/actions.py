"""Source-bound work products, approval decisions, and fail-closed action execution."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4

from app.ai_employees.base import AiActionProposalRecord, now_iso
from app.ai_employees.contracts import (
    AI_EMPLOYEE_EXTERNAL_ACTION_TYPES,
    assert_no_raw_secrets,
    build_ai_employee_action_proposal,
    build_payload_hash,
    get_ai_employee,
)
from app.intake.base import IntakeRecord, RECORD_TYPES
from app.security.policy import Classification


WORK_PRODUCT_RECORD_TYPES = frozenset({"brief", "task", "policy", "note", "document", "action"})
ACTION_DECISIONS = frozenset({"approved", "rejected", "changes_requested", "duplicate"})
PROHIBITED_EXECUTION_TYPES = frozenset({
    "payment",
    "refund",
    "discount",
    "contract_commitment",
    "employee_status_change",
    "compensation_change",
    "permission_change",
    "data_export",
    "data_delete",
    "infrastructure_change",
    "code_change",
})
EXTERNAL_ACTION_EMPLOYEES = {
    "calendar_create_event": frozenset({
        "chief_of_staff", "chief_operating_officer", "operations_manager",
        "people_hr_manager", "sales_partnerships_manager", "customer_success_manager",
    }),
    "calendar_update_event": frozenset({
        "chief_of_staff", "chief_operating_officer", "operations_manager",
        "people_hr_manager", "sales_partnerships_manager", "customer_success_manager",
    }),
    "calendar_cancel_event": frozenset({
        "chief_of_staff", "chief_operating_officer", "operations_manager",
    }),
    "send_email": frozenset({
        "chief_of_staff", "sales_partnerships_manager", "customer_success_manager",
        "people_hr_manager", "legal_compliance_manager",
    }),
    "send_chat": frozenset({"chief_of_staff", "operations_manager", "people_hr_manager"}),
    "publish_social": frozenset({"chief_marketing_officer", "social_media_manager"}),
    "publish_content": frozenset({"chief_marketing_officer", "marketing_strategy_manager"}),
}
APPROVER_ROLE_IDS = {
    "account_admin": frozenset({"admin"}),
    "strategy_owner": frozenset({"admin"}),
    "operations_owner": frozenset({"admin", "location_manager"}),
    "finance_owner": frozenset({"admin", "finance"}),
    "legal_owner": frozenset({"admin"}),
    "people_owner": frozenset({"admin", "hr"}),
    "product_technology_owner": frozenset({"admin"}),
    "product_owner": frozenset({"admin"}),
    "engineering_owner": frozenset({"admin"}),
    "security_owner": frozenset({"admin"}),
    "marketing_owner": frozenset({"admin", "marketing"}),
    "commercial_owner": frozenset({"admin", "location_manager"}),
    "account_owner": frozenset({"admin", "location_manager"}),
}


class ActionExecutor(Protocol):
    target_system: str

    def execute(self, proposal: AiActionProposalRecord, binding) -> str: ...


class ActionExecutorRegistry:
    def __init__(self, executors=()):
        self._executors = {executor.target_system: executor for executor in executors}

    def resolve(self, target_system: str):
        executor = self._executors.get(target_system)
        if not executor:
            raise ValueError(f"No approved executor is configured for {target_system}.")
        return executor


@dataclass(frozen=True)
class WorkProductResult:
    record: IntakeRecord
    audit: IntakeRecord


class AiEmployeeActionService:
    def __init__(self, *, store, intake_store, session_store=None, executor_registry=None):
        self.store = store
        self.intake_store = intake_store
        self.session_store = session_store
        self.executor_registry = executor_registry or ActionExecutorRegistry()

    def create_work_product(
        self,
        *,
        principal,
        employee_id: str,
        record_type: str,
        title: str,
        content: str,
        classification: str,
        source_record_ids: tuple[str, ...],
        mission_id: str = "",
        conversation_id: str = "",
    ) -> WorkProductResult:
        scope = _principal_scope(principal)
        employee = get_ai_employee(employee_id)
        _active_profile(self.store, employee_id, scope)
        if record_type not in WORK_PRODUCT_RECORD_TYPES or record_type not in RECORD_TYPES:
            raise ValueError("Unsupported AI employee work-product type.")
        title = (title or "").strip()
        content = (content or "").strip()
        if not title or not content:
            raise ValueError("AI employee work products require a title and content.")
        assert_no_raw_secrets(content, "work_product.content")
        sources = _accessible_sources(
            self.intake_store, principal, source_record_ids, scope, classification,
        )
        timestamp = now_iso()
        record = self.intake_store.create(IntakeRecord(
            id=f"airc_{uuid4().hex}",
            **scope,
            app_id="ai_employees",
            purpose="ai_employee_action_propose",
            source="ai_employee",
            source_ref=f"{employee_id}:{mission_id or conversation_id or uuid4().hex}",
            record_type=record_type,
            intent="task" if record_type == "task" else "briefing",
            classification=classification,
            confidence=0.8,
            status="approved",
            title=title[:500],
            content=content,
            summary=content[:500],
            extracted_facts={"source_record_ids": list(source_record_ids)},
            metadata={
                "department": employee.department,
                "category": employee.categories[0] if employee.categories else "general",
                "generated_by_employee_id": employee_id,
                "mission_id": mission_id,
                "conversation_id": conversation_id,
                "source_count": len(sources),
                "actionability": "draft_only",
                "risk_level": "low",
            },
            created_at=timestamp,
        ))
        audit = self._audit_record(
            scope=scope,
            actor_id=employee_id,
            action="work_product_created",
            target_id=record.id,
            classification=classification,
            details={"record_type": record_type, "source_count": len(sources)},
        )
        return WorkProductResult(record=record, audit=audit)

    def propose(
        self,
        *,
        principal,
        employee_id: str,
        action_type: str,
        target_system: str,
        risk_level: str,
        classification: str,
        actionability: str,
        source_record_ids: tuple[str, ...],
        payload_summary: str,
        payload: dict,
        idempotency_key: str,
        expires_at: str = "",
        mission_id: str = "",
        conversation_id: str = "",
        run_id: str = "",
    ) -> AiActionProposalRecord:
        scope = _principal_scope(principal)
        employee = get_ai_employee(employee_id)
        _active_profile(self.store, employee_id, scope)
        _authorize_employee_action(employee, action_type)
        _accessible_sources(
            self.intake_store, principal, source_record_ids, scope, classification,
        )
        assert_no_raw_secrets(payload, "action_payload")
        normalized = build_ai_employee_action_proposal(
            employee_id=employee_id,
            action_type=action_type,
            target_system=target_system,
            risk_level=risk_level,
            classification=classification,
            actionability=actionability,
            source_record_ids=source_record_ids,
            payload_summary=payload_summary,
            payload=payload,
            expires_at=_validated_expiry(expires_at),
            idempotency_key=(idempotency_key or "").strip(),
        )
        policy_approved = _private_calendar_automation_allowed(
            self.store, scope, employee_id, normalized, payload,
        )
        if policy_approved:
            normalized = replace(
                normalized,
                requires_approval=False,
                status="approved",
                reason="Explicit private self-only focus automation policy matched.",
            )
        candidate = AiActionProposalRecord(
            id=f"aiap_{uuid4().hex}",
            **scope,
            mission_id=mission_id,
            conversation_id=conversation_id,
            run_id=run_id,
            employee_id=employee_id,
            action_type=normalized.action_type,
            target_system=normalized.target_system,
            risk_level=normalized.risk_level,
            classification=normalized.classification,
            actionability=normalized.actionability,
            source_record_ids=normalized.source_record_ids,
            payload_summary=normalized.payload_summary,
            payload=dict(payload),
            payload_hash=normalized.payload_hash,
            required_approver_role=normalized.required_approver_role,
            expires_at=normalized.expires_at,
            idempotency_key=normalized.idempotency_key,
            status=normalized.status,
            requires_approval=normalized.requires_approval,
            reason=normalized.reason,
            approved_by="policy:private_self_only_focus" if policy_approved else "",
            approved_at=now_iso() if policy_approved else "",
        )
        saved = self.store.save_action_proposal(candidate)
        if saved.id != candidate.id:
            return saved
        self.intake_store.create(IntakeRecord(
            id=f"airc_{uuid4().hex}",
            **scope,
            app_id="ai_employees",
            purpose="ai_employee_action_propose",
            source="ai_employee",
            source_ref=saved.id,
            record_type="action",
            intent="action_proposal",
            classification=saved.classification,
            confidence=1.0,
            status="approved",
            title=saved.payload_summary[:500],
            content=saved.reason,
            summary=saved.payload_summary[:500],
            extracted_facts={"payload": dict(saved.payload)},
            metadata={
                "employee_id": saved.employee_id,
                "department": employee.department,
                "action_type": saved.action_type,
                "target_system": saved.target_system,
                "risk_level": saved.risk_level,
                "actionability": saved.actionability,
                "payload_hash": saved.payload_hash,
                "required_approver_role": saved.required_approver_role,
                "expires_at": saved.expires_at,
                "idempotency_key": saved.idempotency_key,
                "source_record_ids": list(saved.source_record_ids),
            },
            created_at=now_iso(),
        ))
        return saved

    def decide(
        self,
        *,
        principal,
        proposal_id: str,
        decision: str,
        note: str = "",
    ) -> AiActionProposalRecord:
        scope = _principal_scope(principal)
        proposal = self.store.get_action_proposal(proposal_id, **scope)
        if not proposal:
            raise KeyError(f"AI employee action proposal not found: {proposal_id}")
        decision = (decision or "").strip()
        if decision not in ACTION_DECISIONS:
            raise ValueError("Unknown AI employee action decision.")
        if proposal.status not in {"proposed", "changes_requested"}:
            raise ValueError("This AI employee action proposal is no longer reviewable.")
        _require_fresh_human_session(principal, self.session_store)
        _require_approver_role(principal, proposal.required_approver_role)
        if build_payload_hash(proposal.payload) != proposal.payload_hash:
            raise ValueError("The action payload changed after it entered review.")
        if _expired(proposal.expires_at):
            expired = self.store.save_action_proposal(replace(proposal, status="expired"))
            raise ValueError(f"AI employee action approval expired: {expired.id}")
        status = decision
        saved = self.store.save_action_proposal(replace(
            proposal,
            status=status,
            approved_by=principal.user_id if decision == "approved" else "",
            approved_at=now_iso() if decision == "approved" else "",
        ))
        self._audit_record(
            scope=scope,
            actor_id=principal.user_id,
            action=f"action_{decision}",
            target_id=saved.id,
            classification=saved.classification,
            details={
                "payload_hash": saved.payload_hash,
                "required_approver_role": saved.required_approver_role,
                "note": (note or "")[:500],
            },
        )
        return saved

    def execute(self, *, principal, proposal_id: str) -> AiActionProposalRecord:
        scope = _principal_scope(principal, human_required=False)
        proposal = self.store.get_action_proposal(proposal_id, **scope)
        if not proposal:
            raise KeyError(f"AI employee action proposal not found: {proposal_id}")
        if proposal.status == "executed":
            return proposal
        if proposal.status != "approved" or not proposal.approved_by:
            raise PermissionError("AI employee action execution requires a valid human approval.")
        if proposal.action_type in PROHIBITED_EXECUTION_TYPES:
            return self.store.save_action_proposal(replace(
                proposal, status="blocked_by_policy", reason="This action type cannot be executed autonomously.",
            ))
        if _expired(proposal.expires_at):
            return self.store.save_action_proposal(replace(proposal, status="expired"))
        if build_payload_hash(proposal.payload) != proposal.payload_hash:
            return self.store.save_action_proposal(replace(
                proposal, status="blocked_by_policy", reason="Approved payload hash no longer matches.",
            ))
        _active_profile(self.store, proposal.employee_id, scope)
        binding = next((row for row in self.store.list_connector_bindings(**scope)
                        if row.provider == proposal.target_system
                        and row.status == "active"
                        and proposal.employee_id in row.employee_ids
                        and proposal.action_type in row.capabilities), None)
        if not binding:
            return self.store.save_action_proposal(replace(
                proposal, status="blocked_by_policy", reason="No live connector capability grant matches.",
            ))
        executor = self.executor_registry.resolve(proposal.target_system)
        try:
            execution_ref = executor.execute(proposal, binding)
        except Exception:
            return self.store.save_action_proposal(replace(
                proposal, status="execution_failed", reason="External action execution failed.",
            ))
        saved = self.store.save_action_proposal(replace(
            proposal, status="executed", execution_ref=execution_ref,
        ))
        self._audit_record(
            scope=scope,
            actor_id=principal.user_id,
            action="action_executed",
            target_id=saved.id,
            classification=saved.classification,
            details={
                "payload_hash": saved.payload_hash,
                "target_system": saved.target_system,
                "execution_ref": saved.execution_ref,
            },
        )
        return saved

    def _audit_record(self, *, scope, actor_id, action, target_id, classification, details):
        assert_no_raw_secrets(details, "action_audit")
        return self.intake_store.create(IntakeRecord(
            id=f"aiaud_{uuid4().hex}",
            **scope,
            app_id="ai_employees",
            purpose="ai_employee_action_approve",
            source="ai_employees_policy",
            source_ref=target_id,
            record_type="action_audit",
            intent="approval" if "approved" in action else "execution",
            classification=classification,
            confidence=1.0,
            status="approved",
            title=action.replace("_", " ").title(),
            content=f"Policy event {action} for {target_id}.",
            summary=f"{action}:{target_id}",
            extracted_facts={},
            metadata={"actor_id": actor_id, "action": action, "target_id": target_id, **details},
            created_at=now_iso(),
        ))


def _principal_scope(principal, *, human_required: bool = True) -> dict[str, str]:
    if human_required and principal.principal_type != "human":
        raise PermissionError("A human session is required.")
    if (
        not principal.account_id
        or not principal.space_ids
        or len(principal.space_ids) != 1
        or principal.account_id != principal.tenant_id
    ):
        raise PermissionError("AI employee actions require one explicit account and space.")
    return {
        "tenant_id": principal.tenant_id,
        "account_id": principal.account_id,
        "space_id": next(iter(principal.space_ids)),
    }


def _active_profile(store, employee_id: str, scope: dict):
    profile = store.get_profile(employee_id, **scope)
    if not profile or profile.status != "active":
        raise ValueError("AI employee is paused or unavailable.")
    return profile


def _authorize_employee_action(employee, action_type: str) -> None:
    action_type = (action_type or "").strip()
    if action_type in employee.safe_actions:
        return
    if action_type in AI_EMPLOYEE_EXTERNAL_ACTION_TYPES:
        allowed = EXTERNAL_ACTION_EMPLOYEES.get(action_type, frozenset())
        if employee.id in allowed or action_type in PROHIBITED_EXECUTION_TYPES:
            return
    raise PermissionError(f"{employee.role} is not granted the {action_type} capability.")


def _private_calendar_automation_allowed(store, scope, employee_id, proposal, payload) -> bool:
    if (
        proposal.action_type != "calendar_create_event"
        or proposal.target_system != "google_calendar"
        or proposal.actionability != "automation_allowed"
        or proposal.risk_level != "low"
        or proposal.classification not in {"public", "internal"}
    ):
        return False
    from app.ai_employees.connectors.google_calendar import is_private_self_only_focus_payload

    if not is_private_self_only_focus_payload(payload):
        return False
    calendar_id = str(payload.get("calendar_id") or "")
    return any(
        row.provider == "google_calendar"
        and row.status == "active"
        and employee_id in row.employee_ids
        and "calendar_create_private_focus" in row.capabilities
        and "calendar_create_event" in row.capabilities
        and calendar_id in row.resource_ids
        for row in store.list_connector_bindings(**scope)
    )


def _accessible_sources(intake_store, principal, record_ids, scope, classification):
    if not record_ids:
        raise ValueError("AI employee work requires at least one approved source record.")
    requested_classification = Classification.parse(classification)
    sources = []
    highest = Classification.PUBLIC
    for record_id in record_ids:
        record = intake_store.get(record_id, **scope)
        if not record or record.status != "approved":
            raise PermissionError(f"AI employee source record is unavailable: {record_id}")
        source_classification = Classification.parse(record.classification)
        if source_classification > principal.clearance:
            raise PermissionError(f"AI employee source record is unavailable: {record_id}")
        category = record.metadata.get("category", "general")
        if principal.categories is not None and category != "general" and category not in principal.categories:
            raise PermissionError(f"AI employee source record is unavailable: {record_id}")
        highest = max(highest, source_classification)
        sources.append(record)
    if requested_classification < highest:
        raise ValueError("Generated work cannot lower the classification of its source records.")
    return tuple(sources)


def _validated_expiry(value: str) -> str:
    if not value:
        target = datetime.now(timezone.utc) + timedelta(hours=24)
    else:
        target = _parse_time(value)
    now = datetime.now(timezone.utc)
    if target <= now or target > now + timedelta(days=30):
        raise ValueError("Action proposal expiry must be in the future and within 30 days.")
    return target.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _expired(value: str) -> bool:
    return _parse_time(value) <= datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Action proposal expiry must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _require_fresh_human_session(principal, session_store, *, max_age_seconds: int = 900) -> None:
    if principal.principal_type != "human" or not principal.session_id or session_store is None:
        raise PermissionError("A fresh human session is required to approve AI employee actions.")
    session = session_store.get(principal.session_id)
    if not session or not session.active or session.user_id != principal.user_id:
        raise PermissionError("A fresh human session is required to approve AI employee actions.")
    created_at = _parse_time(session.created_at)
    if datetime.now(timezone.utc) - created_at > timedelta(seconds=max_age_seconds):
        raise PermissionError("Please re-authenticate before approving this AI employee action.")


def _require_approver_role(principal, required_role: str) -> None:
    allowed = APPROVER_ROLE_IDS.get(required_role, frozenset({"admin"}))
    if principal.role_id not in allowed:
        raise PermissionError(f"This action requires the {required_role} approver role.")
