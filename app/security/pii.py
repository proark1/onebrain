"""A lightweight, dependency-free PII / secret scanner used as a fail-closed
floor before content can be published to a lower classification.

It returns TYPES and COUNTS only — never the matched values — so a finding can be
logged or stored without becoming a new PII sink. This is the deterministic first
pass; swap in Microsoft Presidio (self-hosted, German NER) for higher recall.

Note: free-text health/injury terms are deliberately NOT matched here — for a
martial-arts gym they appear constantly in legitimate content, so Art.9 handling
belongs in the structured CRM field policy, not a keyword sweep of the corpus.
"""

from __future__ import annotations

import re
from typing import Dict, List

_PATTERNS = {
    "iban": re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}\b"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b"),
    "phone": re.compile(r"(?:\+49|\b0)(?:[ /-]?\d){8,13}\b"),
    "secret": re.compile(r"(?i)\b(?:api[_-]?key|secret|token|passwor[dt])\b\s*[:=]\s*\S{6,}"),
}
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn_ok(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def scan_pii(text: str) -> List[Dict[str, int]]:
    """Return [{type, count}] for each kind of PII/secret found (empty if clean)."""
    text = text or ""
    findings: List[Dict[str, int]] = []
    for name, pattern in _PATTERNS.items():
        count = len(pattern.findall(text))
        if count:
            findings.append({"type": name, "count": count})
    # Credit cards are Luhn-validated to keep ordinary long digit strings from
    # tripping the gate.
    cards = sum(1 for m in _CARD.finditer(text) if _luhn_ok(m.group()))
    if cards:
        findings.append({"type": "credit_card", "count": cards})
    return findings


def has_pii(text: str) -> bool:
    return bool(scan_pii(text))
