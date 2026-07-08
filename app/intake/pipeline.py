"""Deterministic structured-intake pipeline.

This is deliberately rule-first. The output is auditable and easy to test; an
LLM classifier can later be added behind the same pipeline contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from app.intake.base import INTENTS, RECORD_TYPES, IntakeRecord
from app.security.pii import scan_pii


_DATE_RE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b")
_AMOUNT_RE = re.compile(r"\b(?:\u20ac|EUR\s*)\d+(?:[.,]\d{2})?\b|\b\d+(?:[.,]\d{2})?\s*(?:\u20ac|EUR)\b", re.I)

_INTENT_KEYWORDS = {
    "complaint": ("complaint", "angry", "refund", "broken", "not working", "problem", "cancel"),
    "booking": ("booking", "appointment", "schedule", "reschedule", "reservation", "termin"),
    "sales_lead": ("price", "pricing", "offer", "quote", "buy", "interested", "angebot"),
    "task": ("todo", "follow up", "call back", "remind", "task", "erinner"),
    "knowledge_update": ("policy", "process", "procedure", "handbook", "faq", "opening hours", "support hours"),
}

_TYPE_KEYWORDS = {
    "policy": ("policy", "procedure", "handbook", "rule", "guideline"),
    "task": ("todo", "follow up", "call back", "remind", "task"),
    "contact": ("email:", "phone:", "customer:", "contact:"),
    "transcript": ("transcript", "call transcript", "voice transcript"),
    "document": ("document", "pdf", "upload", "file"),
}


@dataclass(frozen=True)
class IntakeInput:
    tenant_id: str
    account_id: str
    space_id: str
    app_id: str
    purpose: str
    content: str
    title: str = ""
    source: str = "service"
    source_ref: str = ""
    record_type: str = ""
    intent: str = ""
    metadata: dict = field(default_factory=dict)


class IntakePipeline:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings

    def ingest(self, data: IntakeInput) -> IntakeRecord:
        content = data.content.strip()
        if not content:
            raise ValueError("Intake content is required.")

        pii_findings = scan_pii(content)
        if pii_findings and self.settings.pii_phase == "synthetic":
            raise ValueError("PII detected while synthetic-data phase is active.")

        record_type, type_confidence = self._record_type(data.record_type, data.source, content, data.title)
        intent, intent_confidence = self._intent(data.intent, content)
        classification = self._classification(pii_findings)
        confidence = min(type_confidence, intent_confidence)
        status = self._status(classification, confidence)
        summary = self._summary(content)
        extracted_facts = self._facts(content, pii_findings, record_type, intent)

        record = IntakeRecord(
            id=f"rec_{uuid4().hex}",
            tenant_id=data.tenant_id.strip(),
            account_id=data.account_id.strip(),
            space_id=data.space_id.strip(),
            app_id=data.app_id.strip(),
            purpose=data.purpose.strip(),
            source=(data.source or "service").strip(),
            source_ref=data.source_ref.strip(),
            record_type=record_type,
            intent=intent,
            classification=classification,
            confidence=confidence,
            status=status,
            title=data.title.strip() or self._default_title(record_type, intent),
            content=content,
            summary=summary,
            extracted_facts=extracted_facts,
            metadata=dict(data.metadata or {}),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        return self.store.create(record)

    def _record_type(self, explicit: str, source: str, content: str, title: str) -> tuple[str, float]:
        explicit = (explicit or "").strip()
        if explicit in RECORD_TYPES:
            return explicit, 0.98
        text = f"{source} {title} {content}".lower()
        if "communication" in source or "whatsapp" in text or "telegram" in text or "sender:" in text:
            return "message", 0.9
        for record_type, keywords in _TYPE_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                return record_type, 0.82
        return "note", 0.62

    def _intent(self, explicit: str, content: str) -> tuple[str, float]:
        explicit = (explicit or "").strip()
        if explicit in INTENTS:
            return explicit, 0.98
        text = content.lower()
        for intent, keywords in _INTENT_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                return intent, 0.82
        if "?" in content or text.startswith(("what ", "how ", "when ", "where ", "why ", "can ", "do ")):
            return "question", 0.78
        return "internal_note", 0.58

    def _classification(self, pii_findings: list[dict]) -> str:
        sensitive = {"iban", "credit_card", "secret"}
        if any(finding["type"] in sensitive for finding in pii_findings):
            return "restricted"
        if pii_findings:
            return "confidential"
        return "internal"

    def _status(self, classification: str, confidence: float) -> str:
        if self.settings.require_approval or classification in {"confidential", "restricted"} or confidence < 0.6:
            return "pending"
        return "approved"

    def _summary(self, content: str) -> str:
        compact = " ".join(content.split())
        return compact[:240]

    def _facts(self, content: str, pii_findings: list[dict], record_type: str, intent: str) -> dict:
        text = content.lower()
        signals = sorted({
            intent,
            *[
                label
                for label, keywords in _INTENT_KEYWORDS.items()
                if any(keyword in text for keyword in keywords)
            ],
        })
        facts = {
            "record_type": record_type,
            "intent": intent,
            "signals": signals,
            "pii_findings": pii_findings,
            "dates": _DATE_RE.findall(content)[:10],
            "amounts": _AMOUNT_RE.findall(content)[:10],
            "characters": len(content),
        }
        return facts

    def _default_title(self, record_type: str, intent: str) -> str:
        return f"{record_type.replace('_', ' ')} / {intent.replace('_', ' ')}"
