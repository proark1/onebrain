"""The pre-publication PII/secret scanner: high-precision on structured PII,
quiet on ordinary public content, and Luhn-guarded on card numbers.
"""

from __future__ import annotations

from app.security.pii import has_pii, scan_pii


def _types(text):
    return {f["type"] for f in scan_pii(text)}


def test_detects_email_and_iban():
    t = _types("Contact anna.muster@example.de, IBAN DE89 3704 0044 0532 0130 00")
    assert "email" in t and "iban" in t


def test_detects_secret_assignment():
    assert has_pii("api_key = sk-9f8a7b6c5d4e3f2a1b0c")
    assert has_pii("Password: hunter2hunter2")


def test_clean_public_text_is_silent():
    # A typical PUBLIC gym doc: opening hours and prices, no personal data.
    text = "Open Monday to Friday 06:00 to 23:00. Membership is 49 EUR per month. Day pass 15 EUR."
    assert scan_pii(text) == []


def test_luhn_reduces_card_false_positives():
    assert any(f["type"] == "credit_card" for f in scan_pii("card 4242 4242 4242 4242"))   # valid Luhn
    assert all(f["type"] != "credit_card" for f in scan_pii("ref 4242 4242 4242 4243"))    # invalid Luhn


def test_findings_carry_no_raw_values():
    # Findings are types + counts only, never the matched PII (no new PII sink).
    for f in scan_pii("mail a@b.de and b@c.de"):
        assert set(f.keys()) == {"type", "count"}
        assert isinstance(f["count"], int)
