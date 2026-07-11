"""Structured intake records and store contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol


RECORD_TYPES = frozenset({
    "action",
    "action_audit",
    "assistant_setting",
    "brief",
    "calendar_event",
    "calendar_focus_plan",
    "contact",
    "document",
    "fact",
    "feedback",
    "follow_up",
    "message",
    "model_usage",
    "note",
    "notification_event",
    "notification_preference",
    "policy",
    "policy_decision",
    "provider_account",
    "provider_health",
    "scope_grant",
    "secret_reference",
    "security_decision",
    "sync_cursor",
    "sync_subscription",
    "task",
    "telegram_binding",
    "transcript",
    "voice_transcript",
})
INTENTS = frozenset({
    "action_proposal",
    "approval",
    "briefing",
    "question",
    "complaint",
    "booking",
    "calendar_focus",
    "connected_account",
    "execution",
    "feedback",
    "follow_up",
    "model_usage",
    "notification",
    "provider_health",
    "sales_lead",
    "security_decision",
    "settings_update",
    "sync_state",
    "telegram_binding",
    "task",
    "knowledge_update",
    "internal_note",
    "voice_turn",
})
INTAKE_STATUSES = frozenset({"approved", "pending", "archived"})


@dataclass(frozen=True)
class IntakeRecord:
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    app_id: str
    purpose: str
    source: str
    source_ref: str
    record_type: str
    intent: str
    classification: str
    confidence: float
    status: str
    title: str
    content: str
    summary: str
    extracted_facts: Dict = field(default_factory=dict)
    metadata: Dict = field(default_factory=dict)
    created_at: str = ""


class IntakeStore(Protocol):
    def create(self, record: IntakeRecord) -> IntakeRecord: ...

    def get(
        self,
        record_id: str,
        tenant_id: str = "",
        account_id: str = "",
        space_id: str = "",
    ) -> Optional[IntakeRecord]: ...

    def list_by_scope(self, tenant_id: str, account_id: str = "", space_id: str = "") -> List[IntakeRecord]: ...

    def export_records(self, tenant_id: str, account_id: str = "", space_id: str = "") -> List[dict]: ...

    def delete_records_by_scope(self, tenant_id: str, account_id: str = "", space_id: str = "",
                                older_than: str = "") -> int: ...

    def delete_by_source_ref(
        self, tenant_id: str, source_ref: str, account_id: str = "", space_id: str = "",
    ) -> int: ...

    def count(self) -> int: ...
