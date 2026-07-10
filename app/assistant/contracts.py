"""OneBrain contracts used by the assistant module.

These names are intentionally data-layer contracts, not assistant-owned tables.
Records stay in intake_records and audit facts stay in platform_audit_events.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy


ASSISTANT_APP_ID = "assistant"
ASSISTANT_CONTRACT_VERSION = "assistant.v1"

ASSISTANT_PURPOSES = frozenset({
    "assistant_action",
    "assistant_briefing",
    "assistant_calendar_planning",
    "assistant_connected_account",
    "assistant_context",
    "assistant_feedback",
    "assistant_followup",
    "assistant_model_usage",
    "assistant_notification",
    "assistant_provider_health",
    "assistant_security",
    "assistant_settings",
    "assistant_sync",
    "assistant_voice",
    "assistant_workday",
})

ASSISTANT_RECORD_TYPES = frozenset({
    "action",
    "action_audit",
    "assistant_setting",
    "brief",
    "calendar_event",
    "calendar_focus_plan",
    "calendar_insight",
    "feedback",
    "follow_up",
    "follow_up_risk",
    "inbox_triage",
    "message",
    "model_usage",
    "notification_event",
    "notification_preference",
    "policy_decision",
    "priority_item",
    "provider_account",
    "provider_calendar_event",
    "provider_health",
    "provider_message",
    "scope_grant",
    "secret_reference",
    "security_decision",
    "sync_cursor",
    "sync_subscription",
    "task",
    "teaching_signal",
    "telegram_binding",
    "transcript",
    "voice_transcript",
    "workday_brief",
})

ASSISTANT_INTENTS = frozenset({
    "action_proposal",
    "approval",
    "briefing",
    "calendar_focus",
    "calendar_insight",
    "connected_account",
    "execution",
    "feedback",
    "follow_up",
    "inbox_triage",
    "model_usage",
    "notification",
    "priority",
    "provider_health",
    "provider_source",
    "security_decision",
    "settings_update",
    "sync_state",
    "telegram_binding",
    "voice_turn",
    "workday",
})

_DEFAULT_INTENTS = {
    "action": "action_proposal",
    "action_audit": "execution",
    "assistant_setting": "settings_update",
    "brief": "briefing",
    "calendar_event": "calendar_focus",
    "calendar_focus_plan": "calendar_focus",
    "calendar_insight": "calendar_insight",
    "feedback": "feedback",
    "follow_up": "follow_up",
    "follow_up_risk": "follow_up",
    "inbox_triage": "inbox_triage",
    "message": "notification",
    "model_usage": "model_usage",
    "notification_event": "notification",
    "notification_preference": "notification",
    "policy_decision": "security_decision",
    "priority_item": "priority",
    "provider_account": "connected_account",
    "provider_calendar_event": "provider_source",
    "provider_health": "provider_health",
    "provider_message": "provider_source",
    "scope_grant": "connected_account",
    "secret_reference": "connected_account",
    "security_decision": "security_decision",
    "sync_cursor": "sync_state",
    "sync_subscription": "sync_state",
    "task": "follow_up",
    "teaching_signal": "feedback",
    "telegram_binding": "telegram_binding",
    "transcript": "voice_turn",
    "voice_transcript": "voice_turn",
    "workday_brief": "workday",
}

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

_SECRET_REFERENCE_KEYS = frozenset({
    "secret_ref",
    "secret_ref_id",
    "secret_reference",
    "secret_reference_id",
    "secret_version",
    "secret_provider",
})


def default_assistant_intent(record_type: str) -> str:
    return _DEFAULT_INTENTS.get((record_type or "").strip(), "")


def build_assistant_metadata(
    record_type: str,
    purpose: str,
    intent: str,
    *,
    metadata: Mapping | None = None,
    provenance: Mapping | None = None,
    retention: Mapping | None = None,
) -> dict:
    """Validate and wrap assistant metadata without storing raw secrets."""

    record_type = (record_type or "").strip()
    purpose = (purpose or "").strip()
    intent = (intent or default_assistant_intent(record_type)).strip()
    _validate_contract_names(record_type, purpose, intent)

    clean_metadata = _copy_mapping(metadata, "metadata")
    clean_provenance = _copy_mapping(provenance, "provenance")
    clean_retention = _copy_mapping(retention, "retention")
    _assert_no_raw_secrets(clean_metadata, "metadata")
    _assert_no_raw_secrets(clean_provenance, "provenance")
    _assert_no_raw_secrets(clean_retention, "retention")
    if record_type == "secret_reference" and not _contains_secret_reference(clean_metadata):
        raise ValueError("secret_reference records must identify a secret_ref, not a raw secret value.")

    clean_metadata["assistant_contract"] = {
        "version": ASSISTANT_CONTRACT_VERSION,
        "app_id": ASSISTANT_APP_ID,
        "record_type": record_type,
        "purpose": purpose,
        "intent": intent,
        "provenance": clean_provenance,
        "retention": clean_retention,
    }
    return clean_metadata


def build_assistant_audit_meta(metadata: Mapping | None = None) -> dict:
    clean_metadata = _copy_mapping(metadata, "metadata")
    _assert_no_raw_secrets(clean_metadata, "metadata")
    clean_metadata["assistant_contract"] = {
        "version": ASSISTANT_CONTRACT_VERSION,
        "app_id": ASSISTANT_APP_ID,
        "record_type": "action_audit",
    }
    return clean_metadata


def validate_assistant_purpose(purpose: str) -> str:
    purpose = (purpose or "").strip()
    if purpose not in ASSISTANT_PURPOSES:
        raise ValueError(f"Unknown assistant purpose: {purpose}")
    return purpose


def validate_assistant_audit_action(action: str) -> str:
    action = (action or "").strip()
    if not action:
        raise ValueError("Assistant audit action is required.")
    if not action.startswith("assistant."):
        raise ValueError("Assistant audit actions must use the assistant.* namespace.")
    return action


def _validate_contract_names(record_type: str, purpose: str, intent: str) -> None:
    if record_type not in ASSISTANT_RECORD_TYPES:
        raise ValueError(f"Unknown assistant record_type: {record_type}")
    if purpose not in ASSISTANT_PURPOSES:
        raise ValueError(f"Unknown assistant purpose: {purpose}")
    if intent and intent not in ASSISTANT_INTENTS:
        raise ValueError(f"Unknown assistant intent: {intent}")


def _copy_mapping(value: Mapping | None, label: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"Assistant {label} must be an object.")
    return deepcopy(dict(value))


def _assert_no_raw_secrets(value, path: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if _looks_like_raw_secret_key(str(key)):
                raise ValueError(f"Raw secret values are not allowed in OneBrain assistant records: {child_path}")
            _assert_no_raw_secrets(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_raw_secrets(child, f"{path}[{index}]")


def _looks_like_raw_secret_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    if normalized in _SECRET_REFERENCE_KEYS or normalized.endswith("_secret_ref"):
        return False
    if normalized in _RAW_SECRET_KEYS:
        return True
    return (
        normalized.endswith("_access_token")
        or normalized.endswith("_api_key")
        or normalized.endswith("_bot_token")
        or normalized.endswith("_client_secret")
        or normalized.endswith("_password")
        or normalized.endswith("_refresh_token")
        or normalized.endswith("_secret")
        or normalized.endswith("_token")
    )


def _contains_secret_reference(value) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SECRET_REFERENCE_KEYS or normalized.endswith("_secret_ref"):
                return True
            if _contains_secret_reference(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_reference(child) for child in value)
    return False
