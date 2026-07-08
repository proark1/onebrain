"""Structured intake records and store contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol


RECORD_TYPES = frozenset({"message", "document", "contact", "task", "fact", "policy", "note", "transcript"})
INTENTS = frozenset({
    "question",
    "complaint",
    "booking",
    "sales_lead",
    "task",
    "knowledge_update",
    "internal_note",
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

    def get(self, record_id: str) -> Optional[IntakeRecord]: ...

    def list_by_scope(self, tenant_id: str, account_id: str = "", space_id: str = "") -> List[IntakeRecord]: ...

    def export_records(self, tenant_id: str, account_id: str = "", space_id: str = "") -> List[dict]: ...

    def delete_records_by_scope(self, tenant_id: str, account_id: str = "", space_id: str = "") -> int: ...

    def count(self) -> int: ...
