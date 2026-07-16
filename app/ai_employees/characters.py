"""Versioned administrator customization for AI employee characters."""

from __future__ import annotations

from app.ai_employees.base import default_character_payload
from app.ai_employees.contracts import assert_no_raw_secrets, get_ai_employee


EDITABLE_CHARACTER_FIELDS = frozenset({
    "display_name",
    "fictional_age",
    "country",
    "pronouns",
    "biography",
    "avatar_url",
    "personality",
    "tone",
    "vocabulary",
    "communication_style",
    "strengths",
    "watch_outs",
    "working_style",
    "collaboration_behavior",
    "role_focus",
    "character_prompt",
    "examples",
})

_LIST_FIELDS = {"personality", "strengths", "watch_outs", "examples"}
_MAX_LENGTHS = {
    "display_name": 160,
    "country": 120,
    "pronouns": 80,
    "biography": 2_000,
    "avatar_url": 1_000,
    "tone": 1_000,
    "vocabulary": 2_000,
    "communication_style": 2_000,
    "working_style": 3_000,
    "collaboration_behavior": 3_000,
    "role_focus": 3_000,
    "character_prompt": 12_000,
}


def merge_character_patch(base_payload: dict, patch: dict) -> dict:
    unknown = sorted(set(patch) - EDITABLE_CHARACTER_FIELDS)
    if unknown:
        raise ValueError(f"Character fields are not editable: {', '.join(unknown)}")
    merged = {**base_payload, **patch}
    validate_character_payload(merged)
    return merged


def validate_character_payload(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError("Character payload must be an object.")
    unknown = sorted(set(payload) - EDITABLE_CHARACTER_FIELDS)
    if unknown:
        raise ValueError(f"Character fields are not editable: {', '.join(unknown)}")
    assert_no_raw_secrets(payload, "character")
    display_name = str(payload.get("display_name") or "").strip()
    if not display_name:
        raise ValueError("Character display_name is required.")
    age = payload.get("fictional_age")
    if not isinstance(age, int) or not 18 <= age <= 80:
        raise ValueError("Character fictional_age must be between 18 and 80.")
    for field, maximum in _MAX_LENGTHS.items():
        value = payload.get(field, "")
        if not isinstance(value, str) or len(value) > maximum:
            raise ValueError(f"Character {field} must be text with at most {maximum} characters.")
    for field in _LIST_FIELDS:
        value = payload.get(field, [])
        if not isinstance(value, list) or len(value) > 20:
            raise ValueError(f"Character {field} must be a list with at most 20 items.")
        if any(not isinstance(item, str) or len(item) > 500 for item in value):
            raise ValueError(f"Character {field} items must be text with at most 500 characters.")


def create_character_draft(
    store,
    *,
    tenant_id: str,
    account_id: str,
    space_id: str,
    employee_id: str,
    patch: dict,
    author_id: str,
):
    profile = store.get_profile(
        employee_id, tenant_id=tenant_id, account_id=account_id, space_id=space_id,
    )
    if not profile:
        raise KeyError(f"AI employee not found: {employee_id}")
    current = store.get_character_version(
        profile.default_version_id,
        tenant_id=tenant_id,
        account_id=account_id,
        space_id=space_id,
    )
    if not current:
        raise ValueError("Published AI employee character version is missing.")
    payload = merge_character_patch(current.payload, patch)
    return store.create_character_draft(
        tenant_id=tenant_id,
        account_id=account_id,
        space_id=space_id,
        employee_id=employee_id,
        payload=payload,
        author_id=author_id,
        base_version_id=current.id,
    )


def publish_character_version(
    store,
    version_id: str,
    *,
    tenant_id: str,
    account_id: str,
    space_id: str,
    actor_id: str,
    expected_profile_version_id: str,
):
    return store.publish_character_version(
        version_id,
        tenant_id=tenant_id,
        account_id=account_id,
        space_id=space_id,
        actor_id=actor_id,
        expected_profile_version_id=expected_profile_version_id,
    )


def rollback_character(
    store,
    *,
    tenant_id: str,
    account_id: str,
    space_id: str,
    employee_id: str,
    source_version_id: str,
    actor_id: str,
    expected_profile_version_id: str,
):
    source = store.get_character_version(
        source_version_id,
        tenant_id=tenant_id,
        account_id=account_id,
        space_id=space_id,
    )
    if not source or source.employee_id != employee_id or source.state != "published":
        raise ValueError("Rollback source must be a published version for this employee.")
    validate_character_payload(source.payload)
    draft = store.create_character_draft(
        tenant_id=tenant_id,
        account_id=account_id,
        space_id=space_id,
        employee_id=employee_id,
        payload=source.payload,
        author_id=actor_id,
        base_version_id=expected_profile_version_id,
    )
    return publish_character_version(
        store,
        draft.id,
        tenant_id=tenant_id,
        account_id=account_id,
        space_id=space_id,
        actor_id=actor_id,
        expected_profile_version_id=expected_profile_version_id,
    )


def reset_character(
    store,
    *,
    tenant_id: str,
    account_id: str,
    space_id: str,
    employee_id: str,
    actor_id: str,
    expected_profile_version_id: str,
):
    payload = default_character_payload(get_ai_employee(employee_id))
    validate_character_payload(payload)
    draft = store.create_character_draft(
        tenant_id=tenant_id,
        account_id=account_id,
        space_id=space_id,
        employee_id=employee_id,
        payload=payload,
        author_id=actor_id,
        base_version_id=expected_profile_version_id,
    )
    return publish_character_version(
        store,
        draft.id,
        tenant_id=tenant_id,
        account_id=account_id,
        space_id=space_id,
        actor_id=actor_id,
        expected_profile_version_id=expected_profile_version_id,
    )


def character_preview(employee_id: str, payload: dict) -> str:
    employee = get_ai_employee(employee_id)
    validate_character_payload(payload)
    return (
        f"{payload['display_name']} — {employee.role}\n"
        f"Tone: {payload.get('tone', '')}\n"
        f"Working style: {payload.get('working_style', '')}\n"
        f"Character direction: {payload.get('character_prompt', '')}\n"
        "Immutable role, access, approval, and safety policy remains server-governed."
    )
