"""Contracts for the governed AI Employees module."""

from __future__ import annotations

from collections import Counter

import pytest

from app.ai_employees.contracts import (
    AI_EMPLOYEE_BY_ID,
    AI_EMPLOYEE_NEVER_ACTIONS,
    AI_EMPLOYEE_PODS,
    AI_EMPLOYEE_PURPOSES,
    AI_EMPLOYEES,
    AI_EMPLOYEES_CONTRACT_VERSION,
    AI_EMPLOYEE_PROVIDER_IDS,
    LEADERSHIP_COUNCIL_IDS,
    MAX_MISSION_SQUAD_SIZE,
    approval_required_for_action,
    build_ai_employee_action_proposal,
    build_payload_hash,
    get_ai_employee,
    validate_ai_employee_provider,
    validate_ai_employee_purpose,
    validate_mission_squad,
)


EXPECTED_ROSTER = {
    "chief_of_staff": ("Clara Hoffmann", "AI Chief of Staff", "Germany", "she/her", ""),
    "corporate_strategy_manager": (
        "Oliver Bennett", "Corporate Strategy Manager", "United Kingdom", "he/him", "chief_of_staff",
    ),
    "chief_operating_officer": (
        "Élodie Martin", "Chief Operating Officer", "France", "she/her", "chief_of_staff",
    ),
    "operations_manager": (
        "Felix Wagner", "Operations Manager", "Germany", "he/him", "chief_operating_officer",
    ),
    "finance_manager": (
        "Sophie Laurent", "Finance Manager", "France", "she/her", "chief_operating_officer",
    ),
    "legal_compliance_manager": (
        "James Whitmore", "Legal & Compliance Manager", "United Kingdom", "he/him", "chief_operating_officer",
    ),
    "people_hr_manager": (
        "Hannah Becker", "People & HR Manager", "Germany", "she/her", "chief_operating_officer",
    ),
    "chief_product_technology_officer": (
        "Lukas Schneider", "Chief Product & Technology Officer", "Germany", "he/him", "chief_of_staff",
    ),
    "product_manager": (
        "Camille Moreau", "Product Manager", "France", "she/her", "chief_product_technology_officer",
    ),
    "software_architect": (
        "Thomas Reed", "Software Architect", "United Kingdom", "he/him", "chief_product_technology_officer",
    ),
    "cybersecurity_manager": (
        "Aisha Khan", "Cybersecurity Manager", "United Kingdom", "she/her", "chief_product_technology_officer",
    ),
    "chief_marketing_officer": (
        "Antoine Dubois", "Chief Marketing Officer", "France", "he/him", "chief_of_staff",
    ),
    "marketing_strategy_manager": (
        "Charlotte Evans", "Marketing Strategy Manager", "United Kingdom", "she/her", "chief_marketing_officer",
    ),
    "social_media_manager": (
        "Julien Mercier", "Social Media Manager", "France", "he/him", "chief_marketing_officer",
    ),
    "sales_partnerships_manager": (
        "Maximilian Bauer", "Sales & Partnerships Manager", "Germany", "he/him", "chief_marketing_officer",
    ),
    "customer_success_manager": (
        "Lena Fischer", "Customer Success Manager", "Germany", "she/her", "chief_marketing_officer",
    ),
}


def test_ai_employee_v2_roster_matches_the_approved_organization():
    assert AI_EMPLOYEES_CONTRACT_VERSION == "ai_employees.v2"
    assert len(AI_EMPLOYEES) == 16
    assert len(AI_EMPLOYEE_BY_ID) == 16
    assert set(AI_EMPLOYEE_BY_ID) == set(EXPECTED_ROSTER)

    for employee_id, expected in EXPECTED_ROSTER.items():
        employee = get_ai_employee(employee_id)
        assert (employee.name, employee.role, employee.country, employee.pronouns, employee.reports_to) == expected
        assert 18 <= employee.age <= 80
        assert employee.department
        assert employee.pod
        assert employee.categories
        assert set(employee.purposes) <= AI_EMPLOYEE_PURPOSES
        assert "ai_employee_action_approve" not in employee.purposes
        assert employee.safe_actions
        assert employee.approval_rule
        assert employee.prompt_safe_description
        assert employee.personality
        assert employee.tone
        assert employee.strengths
        assert employee.watch_outs
        assert employee.working_style
        assert employee.default_mode in {"draft", "suggest", "approval_queue"}
        assert employee.owner_role
        assert employee.productivity_metrics
        assert employee.never_without_approval


def test_ai_employee_roster_has_the_approved_country_and_gender_balance():
    assert Counter(employee.country for employee in AI_EMPLOYEES) == {
        "Germany": 6,
        "United Kingdom": 5,
        "France": 5,
    }
    assert Counter(employee.pronouns for employee in AI_EMPLOYEES) == {
        "she/her": 8,
        "he/him": 8,
    }


def test_ai_employee_hierarchy_and_pods_respect_the_six_person_ceiling():
    assert LEADERSHIP_COUNCIL_IDS == (
        "chief_of_staff",
        "chief_operating_officer",
        "chief_product_technology_officer",
        "chief_marketing_officer",
    )
    assert get_ai_employee("corporate_strategy_manager").reports_to == "chief_of_staff"
    assert "corporate_strategy_manager" not in LEADERSHIP_COUNCIL_IDS

    assert AI_EMPLOYEE_PODS == {
        "chief_of_staff_office": ("chief_of_staff", "corporate_strategy_manager"),
        "operations_corporate": (
            "chief_operating_officer",
            "operations_manager",
            "finance_manager",
            "legal_compliance_manager",
            "people_hr_manager",
        ),
        "product_technology_security": (
            "chief_product_technology_officer",
            "product_manager",
            "software_architect",
            "cybersecurity_manager",
        ),
        "market_customer": (
            "chief_marketing_officer",
            "marketing_strategy_manager",
            "social_media_manager",
            "sales_partnerships_manager",
            "customer_success_manager",
        ),
    }
    assert all(len(members) < MAX_MISSION_SQUAD_SIZE for members in AI_EMPLOYEE_PODS.values())
    assert len(LEADERSHIP_COUNCIL_IDS) < MAX_MISSION_SQUAD_SIZE


def test_mission_squad_requires_clara_and_rejects_duplicates_or_a_seventh_member():
    allowed = validate_mission_squad((
        "chief_of_staff",
        "chief_operating_officer",
        "finance_manager",
        "legal_compliance_manager",
        "operations_manager",
        "people_hr_manager",
    ))
    assert len(allowed) == MAX_MISSION_SQUAD_SIZE

    with pytest.raises(ValueError, match="Chief of Staff"):
        validate_mission_squad(("chief_operating_officer", "finance_manager"))

    with pytest.raises(ValueError, match="Duplicate"):
        validate_mission_squad(("chief_of_staff", "finance_manager", "finance_manager"))

    with pytest.raises(ValueError, match="at most six"):
        validate_mission_squad((
            "chief_of_staff",
            "chief_operating_officer",
            "finance_manager",
            "legal_compliance_manager",
            "operations_manager",
            "people_hr_manager",
            "corporate_strategy_manager",
        ))


def test_ai_employee_purposes_and_model_providers_are_explicit_and_fail_closed():
    assert AI_EMPLOYEE_PURPOSES == frozenset({
        "ai_employee_read",
        "ai_employee_configure",
        "ai_employee_mission_run",
        "ai_employee_action_propose",
        "ai_employee_action_approve",
        "ai_employee_connector_manage",
        "ai_employee_action_execute",
    })
    assert AI_EMPLOYEE_PROVIDER_IDS == frozenset({"gemini", "anthropic", "local"})
    assert validate_ai_employee_provider("gemini") == "gemini"

    with pytest.raises(ValueError, match="Unknown AI employee"):
        get_ai_employee("not_real")
    with pytest.raises(ValueError, match="Unknown AI employee purpose"):
        validate_ai_employee_purpose("assistant_action")
    with pytest.raises(ValueError, match="Unknown AI employee provider"):
        validate_ai_employee_provider("mystery")


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
    assert proposal.required_approver_role == "marketing_owner"
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
