"""Contracts for governed AI employees."""

from __future__ import annotations

import pytest

from app.assistant.employees import (
    AI_EMPLOYEE_NEVER_ACTIONS,
    AI_EMPLOYEE_PURPOSES,
    AI_EMPLOYEES,
    approval_required_for_action,
    build_ai_employee_action_proposal,
    build_payload_hash,
    get_ai_employee,
    validate_ai_employee_purpose,
)


def test_ai_employee_roster_has_exactly_eight_guarded_employees():
    assert len(AI_EMPLOYEES) == 8
    assert len({employee.id for employee in AI_EMPLOYEES}) == 8

    for employee in AI_EMPLOYEES:
        assert employee.name
        assert employee.role
        assert employee.department
        assert employee.categories
        assert set(employee.purposes) <= AI_EMPLOYEE_PURPOSES
        assert "ai_employee_action_approve" not in employee.purposes
        assert employee.safe_actions
        assert employee.approval_rule
        assert employee.prompt_safe_description
        assert employee.default_mode in {"draft", "suggest", "approval_queue"}
        assert employee.owner_role
        assert employee.productivity_metrics
        assert employee.never_without_approval


def test_unknown_ai_employee_and_purpose_fail_closed():
    with pytest.raises(ValueError, match="Unknown AI employee"):
        get_ai_employee("not_real")

    with pytest.raises(ValueError, match="Unknown AI employee purpose"):
        validate_ai_employee_purpose("assistant_action")


def test_ai_employee_approval_policy_forces_sensitive_actions_to_humans():
    assert approval_required_for_action(
        actionability="draft_only",
        risk_level="low",
        classification="internal",
    ) is False
    assert approval_required_for_action(
        actionability="approval_required",
        risk_level="low",
        classification="internal",
    ) is True
    assert approval_required_for_action(
        actionability="draft_only",
        risk_level="high",
        classification="internal",
    ) is True
    assert approval_required_for_action(
        actionability="draft_only",
        risk_level="low",
        classification="restricted",
    ) is True


def test_ai_employee_never_actions_capture_hard_security_boundaries():
    assert "autonomous_financial_transaction" in AI_EMPLOYEE_NEVER_ACTIONS
    assert "autonomous_hr_decision" in AI_EMPLOYEE_NEVER_ACTIONS
    assert "privilege_escalation_or_secret_access" in AI_EMPLOYEE_NEVER_ACTIONS


def test_ai_employee_action_proposal_hashes_payload_and_defaults_approver():
    proposal = build_ai_employee_action_proposal(
        employee_id="finance_manager",
        action_type="invoice_followup_draft",
        target_system="onebrain",
        risk_level="low",
        classification="internal",
        actionability="draft_only",
        source_record_ids=("rec_invoice_1",),
        payload_summary="Draft a polite follow-up for invoice INV-1.",
        payload={"invoice_id": "INV-1", "tone": "polite"},
    )

    assert proposal.employee_id == "finance_manager"
    assert proposal.department == "finance"
    assert proposal.required_approver_role == "finance_owner"
    assert proposal.requires_approval is False
    assert proposal.payload_hash == build_payload_hash({"tone": "polite", "invoice_id": "INV-1"})
    assert proposal.idempotency_key.startswith("finance_manager:invoice_followup_draft:")


def test_ai_employee_external_action_proposal_requires_approval():
    proposal = build_ai_employee_action_proposal(
        employee_id="social_media_manager",
        action_type="publish_social",
        target_system="linkedin",
        risk_level="medium",
        classification="internal",
        actionability="draft_only",
        source_record_ids=("rec_campaign_1",),
        payload_summary="Publish approved launch post.",
        payload={"post_id": "launch-1", "channel": "linkedin"},
    )

    assert proposal.requires_approval is True
    assert proposal.required_approver_role == "social_owner"
    assert "external or privileged" in proposal.reason


def test_ai_employee_action_proposal_rejects_raw_secret_payloads():
    with pytest.raises(ValueError, match="Raw secret key"):
        build_ai_employee_action_proposal(
            employee_id="software_architect",
            action_type="incident_checklist",
            target_system="onebrain",
            risk_level="low",
            classification="internal",
            actionability="draft_only",
            source_record_ids=("rec_incident_1",),
            payload_summary="Draft incident checklist.",
            payload={"api_key": "secret-value"},
        )
