"""Governed AI employee roster and action-safety contracts.

The roster is product metadata for routing, UI, and policy decisions. It never
grants data access by itself; callers still need the platform account, space,
purpose, classification, and service-key checks.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


AI_EMPLOYEES_APP_ID = "ai_employees"
AI_EMPLOYEES_CONTRACT_VERSION = "ai_employees.v1"

AI_EMPLOYEE_PURPOSES = frozenset({
    "ai_employee_read",
    "ai_employee_configure",
    "ai_employee_action_propose",
    "ai_employee_action_approve",
})

AI_EMPLOYEE_ACTIONABILITY = frozenset({
    "answer_only",
    "draft_only",
    "approval_required",
    "automation_allowed",
})

AI_EMPLOYEE_RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})

AI_EMPLOYEE_MODES = frozenset({
    "off",
    "observe",
    "suggest",
    "draft",
    "approval_queue",
    "limited_automation",
})

AI_EMPLOYEE_PROPOSAL_STATUSES = frozenset({
    "draft",
    "proposed",
    "approved",
    "rejected",
    "changes_requested",
    "expired",
    "blocked_by_policy",
    "executed",
    "execution_failed",
    "duplicate",
})

AI_EMPLOYEE_EXTERNAL_ACTION_TYPES = frozenset({
    "send_email",
    "send_chat",
    "publish_social",
    "publish_content",
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

AI_EMPLOYEE_NEVER_ACTIONS = (
    "fully_autonomous_external_communication",
    "autonomous_financial_transaction",
    "autonomous_hr_decision",
    "privilege_escalation_or_secret_access",
    "pending_or_restricted_data_bypass",
)


@dataclass(frozen=True)
class AiEmployee:
    id: str
    name: str
    role: str
    department: str
    categories: tuple[str, ...]
    purposes: tuple[str, ...]
    safe_actions: tuple[str, ...]
    approval_rule: str
    prompt_safe_description: str
    default_mode: str
    owner_role: str
    productivity_metrics: tuple[str, ...]
    never_without_approval: tuple[str, ...]


@dataclass(frozen=True)
class AiEmployeeActionProposal:
    employee_id: str
    department: str
    action_type: str
    target_system: str
    risk_level: str
    classification: str
    actionability: str
    source_record_ids: tuple[str, ...]
    payload_summary: str
    payload_hash: str
    required_approver_role: str
    expires_at: str
    idempotency_key: str
    status: str
    requires_approval: bool
    reason: str


AI_EMPLOYEES: tuple[AiEmployee, ...] = (
    AiEmployee(
        id="finance_manager",
        name="Mira Vale",
        role="Finance Manager",
        department="finance",
        categories=("finance", "billing", "revenue"),
        purposes=("ai_employee_read", "ai_employee_action_propose"),
        safe_actions=("budget_variance_draft", "invoice_followup_draft", "cash_risk_alert"),
        approval_rule="Human finance owner approves exports, payments, vendor messages, and financial commitments.",
        prompt_safe_description="Finance-focused draft and risk assistant for approved finance records.",
        default_mode="draft",
        owner_role="finance_owner",
        productivity_metrics=("cash_risk_alerts", "variance_drafts", "invoice_nudges"),
        never_without_approval=("payments", "financial_exports", "vendor_messages", "discount_commitments"),
    ),
    AiEmployee(
        id="hr_manager",
        name="Noah Mercer",
        role="HR Manager",
        department="people",
        categories=("people", "hr", "policy"),
        purposes=("ai_employee_read", "ai_employee_action_propose"),
        safe_actions=("onboarding_plan_draft", "policy_answer_draft", "retention_risk_flag"),
        approval_rule="Human HR owner approves employee-impacting actions, compensation, reviews, or status changes.",
        prompt_safe_description="People-team assistant for approved HR policy, onboarding, and team-process records.",
        default_mode="draft",
        owner_role="hr_owner",
        productivity_metrics=("onboarding_gaps", "policy_drafts", "retention_flags"),
        never_without_approval=("compensation_changes", "employee_status_changes", "review_publishing"),
    ),
    AiEmployee(
        id="product_manager",
        name="Aiko Tan",
        role="Product Manager",
        department="product",
        categories=("product", "customer_feedback", "roadmap"),
        purposes=("ai_employee_read", "ai_employee_action_propose"),
        safe_actions=("prd_draft", "customer_request_cluster", "sprint_priority_proposal"),
        approval_rule="Product lead approves roadmap commitments, public promises, and priority changes.",
        prompt_safe_description="Product discovery assistant for approved customer feedback, KPI, and roadmap records.",
        default_mode="suggest",
        owner_role="product_lead",
        productivity_metrics=("feedback_clusters", "prd_drafts", "kpi_linked_risks"),
        never_without_approval=("roadmap_commitments", "public_feature_promises", "priority_overrides"),
    ),
    AiEmployee(
        id="software_architect",
        name="Elias Frost",
        role="Software Architect",
        department="engineering",
        categories=("engineering", "security", "reliability"),
        purposes=("ai_employee_read", "ai_employee_action_propose"),
        safe_actions=("adr_draft", "incident_checklist", "dependency_risk_map"),
        approval_rule="Engineer approves code, infrastructure, access, production, or secret-related changes.",
        prompt_safe_description="Engineering assistant for approved architecture, reliability, incident, and API records.",
        default_mode="suggest",
        owner_role="engineering_owner",
        productivity_metrics=("adr_drafts", "incident_checklists", "dependency_risks"),
        never_without_approval=("production_changes", "secret_access", "code_merges", "permission_changes"),
    ),
    AiEmployee(
        id="marketing_strategy_manager",
        name="Sofia Reyes",
        role="Marketing Strategy Manager",
        department="marketing",
        categories=("marketing", "campaigns", "positioning"),
        purposes=("ai_employee_read", "ai_employee_action_propose"),
        safe_actions=("launch_brief_draft", "campaign_segment_suggestion", "positioning_test_draft"),
        approval_rule="Human marketer approves publishing, campaign spend, and external claims.",
        prompt_safe_description="Marketing strategy assistant for approved campaign, positioning, and KPI records.",
        default_mode="draft",
        owner_role="marketing_owner",
        productivity_metrics=("launch_briefs", "segment_ideas", "positioning_tests"),
        never_without_approval=("campaign_spend", "campaign_publishing", "external_claims"),
    ),
    AiEmployee(
        id="social_media_manager",
        name="Kai Morgan",
        role="Social Media Manager",
        department="marketing",
        categories=("marketing", "social", "community"),
        purposes=("ai_employee_read", "ai_employee_action_propose"),
        safe_actions=("post_calendar_draft", "creator_brief_draft", "reply_suggestion_queue"),
        approval_rule="Human social owner approves external posts, public replies, and publishing.",
        prompt_safe_description="Social media assistant for approved brand, channel, and community records.",
        default_mode="approval_queue",
        owner_role="social_owner",
        productivity_metrics=("post_drafts", "reply_suggestions", "trend_windows"),
        never_without_approval=("auto_publish", "public_replies", "crisis_responses"),
    ),
    AiEmployee(
        id="operations_manager",
        name="Priya Nair",
        role="Operations Manager",
        department="operations",
        categories=("operations", "sops", "support"),
        purposes=("ai_employee_read", "ai_employee_action_propose"),
        safe_actions=("sop_update_draft", "escalation_task_draft", "bottleneck_summary"),
        approval_rule="Operations owner approves supplier, staffing, or customer-impacting workflow changes.",
        prompt_safe_description="Operations assistant for approved SOP, handoff, SLA, and process records.",
        default_mode="draft",
        owner_role="operations_owner",
        productivity_metrics=("sla_risks", "sop_drafts", "handoff_blockers"),
        never_without_approval=("staffing_changes", "supplier_terms", "customer_sla_changes"),
    ),
    AiEmployee(
        id="customer_success_manager",
        name="Owen Blake",
        role="Customer Success Manager",
        department="customer_success",
        categories=("customer_success", "accounts", "renewals"),
        purposes=("ai_employee_read", "ai_employee_action_propose"),
        safe_actions=("qbr_note_draft", "renewal_nudge_draft", "escalation_path_flag"),
        approval_rule="Account owner approves external sends, commercial terms, discounts, and commitments.",
        prompt_safe_description="Customer-success assistant for approved account, ticket, renewal, and health records.",
        default_mode="draft",
        owner_role="account_owner",
        productivity_metrics=("health_alerts", "qbr_drafts", "renewal_nudges"),
        never_without_approval=("discounts", "contract_changes", "customer_promises"),
    ),
)

AI_EMPLOYEE_BY_ID = {employee.id: employee for employee in AI_EMPLOYEES}


def get_ai_employee(employee_id: str) -> AiEmployee:
    employee_id = (employee_id or "").strip()
    try:
        return AI_EMPLOYEE_BY_ID[employee_id]
    except KeyError as exc:
        raise ValueError(f"Unknown AI employee: {employee_id}") from exc


def validate_ai_employee_purpose(purpose: str) -> str:
    purpose = (purpose or "").strip()
    if purpose not in AI_EMPLOYEE_PURPOSES:
        raise ValueError(f"Unknown AI employee purpose: {purpose}")
    return purpose


def validate_ai_employee_actionability(value: str) -> str:
    value = (value or "").strip()
    if value not in AI_EMPLOYEE_ACTIONABILITY:
        raise ValueError(f"Unknown AI employee actionability: {value}")
    return value


def validate_ai_employee_risk_level(value: str) -> str:
    value = (value or "").strip()
    if value not in AI_EMPLOYEE_RISK_LEVELS:
        raise ValueError(f"Unknown AI employee risk level: {value}")
    return value


def validate_ai_employee_mode(value: str) -> str:
    value = (value or "").strip()
    if value not in AI_EMPLOYEE_MODES:
        raise ValueError(f"Unknown AI employee mode: {value}")
    return value


def validate_ai_employee_proposal_status(value: str) -> str:
    value = (value or "").strip()
    if value not in AI_EMPLOYEE_PROPOSAL_STATUSES:
        raise ValueError(f"Unknown AI employee proposal status: {value}")
    return value


def approval_required_for_action(*, actionability: str, risk_level: str, classification: str) -> bool:
    """Return whether a proposed action must enter the human approval queue."""

    actionability = validate_ai_employee_actionability(actionability)
    risk_level = validate_ai_employee_risk_level(risk_level)
    classification = (classification or "").strip().lower()
    if actionability == "approval_required":
        return True
    if risk_level in {"high", "critical"}:
        return True
    if classification in {"confidential", "restricted"}:
        return True
    return False


def build_payload_hash(payload: Mapping[str, Any]) -> str:
    clean_payload = _copy_mapping(payload, "payload")
    _assert_no_raw_secrets(clean_payload, "payload")
    encoded = json.dumps(clean_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_ai_employee_action_proposal(
    *,
    employee_id: str,
    action_type: str,
    target_system: str,
    risk_level: str,
    classification: str,
    actionability: str,
    source_record_ids: tuple[str, ...],
    payload_summary: str,
    payload: Mapping[str, Any],
    required_approver_role: str = "",
    expires_at: str = "",
    idempotency_key: str = "",
) -> AiEmployeeActionProposal:
    employee = get_ai_employee(employee_id)
    action_type = _required_string(action_type, "action_type")
    target_system = _required_string(target_system, "target_system")
    risk_level = validate_ai_employee_risk_level(risk_level)
    actionability = validate_ai_employee_actionability(actionability)
    classification = (classification or "").strip().lower()
    if classification not in {"public", "internal", "confidential", "restricted"}:
        raise ValueError(f"Unknown classification: {classification}")
    source_record_ids = tuple(_required_string(record_id, "source_record_id") for record_id in source_record_ids)
    if not source_record_ids:
        raise ValueError("AI employee proposals require at least one source record.")
    payload_summary = _required_string(payload_summary, "payload_summary")
    payload_hash = build_payload_hash(payload)
    required_approver_role = (required_approver_role or employee.owner_role).strip()
    expires_at = expires_at or _default_expiry()
    idempotency_key = idempotency_key or f"{employee.id}:{action_type}:{payload_hash[:16]}"
    requires_approval = approval_required_for_action(
        actionability=actionability,
        risk_level=risk_level,
        classification=classification,
    ) or action_type in AI_EMPLOYEE_EXTERNAL_ACTION_TYPES
    reason = _approval_reason(
        action_type=action_type,
        actionability=actionability,
        risk_level=risk_level,
        classification=classification,
        external=action_type in AI_EMPLOYEE_EXTERNAL_ACTION_TYPES,
    )
    return AiEmployeeActionProposal(
        employee_id=employee.id,
        department=employee.department,
        action_type=action_type,
        target_system=target_system,
        risk_level=risk_level,
        classification=classification,
        actionability=actionability,
        source_record_ids=source_record_ids,
        payload_summary=payload_summary,
        payload_hash=payload_hash,
        required_approver_role=required_approver_role,
        expires_at=expires_at,
        idempotency_key=idempotency_key,
        status="proposed",
        requires_approval=requires_approval,
        reason=reason,
    )


def _approval_reason(*, action_type: str, actionability: str, risk_level: str, classification: str, external: bool) -> str:
    if external:
        return f"{action_type} targets an external or privileged system and requires human approval."
    if actionability == "approval_required":
        return "The source data or proposed action is marked approval_required."
    if risk_level in {"high", "critical"}:
        return f"{risk_level} risk actions require human approval."
    if classification in {"confidential", "restricted"}:
        return f"{classification} data requires human approval before execution."
    return "Low-risk internal proposal can remain draft-first and auditable."


def _default_expiry() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _required_string(value: str, field_name: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{field_name} is required.")
    return value


def _copy_mapping(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object.")
    return dict(value)


_RAW_SECRET_KEYS = frozenset({
    "access_token",
    "api_key",
    "authorization",
    "bot_token",
    "client_secret",
    "cookie",
    "oauth_token",
    "password",
    "refresh_token",
    "secret",
    "secret_value",
    "token",
    "webhook_secret",
})


def _assert_no_raw_secrets(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key).lower()
            if key_text in _RAW_SECRET_KEYS:
                raise ValueError(f"Raw secret key not allowed in {path}.{key}. Use a secret reference.")
            _assert_no_raw_secrets(nested, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _assert_no_raw_secrets(nested, f"{path}[{index}]")
