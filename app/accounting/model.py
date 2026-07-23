"""Accounting domain model — the shapes that flow through Phase 1.

``ExtractedInvoice`` is what the vision extractor returns from one document; the
store persists it (plus validation flags and booking proposals) as one
``accounting_documents`` row and N ``accounting_line_items`` rows. Money is
``Decimal`` end-to-end — invoices are arithmetic and floats would drift the
Rechenprobe (net + tax = gross).

Nothing here talks to the model provider, Drive, Postgres, or the router; it is
plain data + light helpers so validation, booking, and tests can use it in
isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional


# Direction is the single most consequential field: it decides expense-vs-revenue
# accounts, and input-vs-output VAT in the overview. The extractor guesses it, but
# it is always human-confirmable (§2 "immer bestätigen").
INCOMING = "incoming"  # Eingangsrechnung — an expense we received (Vorsteuer)
OUTGOING = "outgoing"  # Ausgangsrechnung — revenue we issued (Umsatzsteuer)

# Kleinbetragsrechnung threshold (§33 UStDV): at/under this gross total the §14
# mandatory-field set is relaxed (no recipient, no separate net/tax required).
SMALL_AMOUNT_GROSS_LIMIT = Decimal("250.00")


def to_decimal(value) -> Optional[Decimal]:
    """Coerce model/JSON output to Decimal, tolerating strings and ``None``.

    Vision models emit numbers as strings or floats and sometimes with a stray
    currency symbol or German decimal comma; normalise rather than trust. Returns
    ``None`` for anything that isn't a finite number so callers can flag a gap
    instead of crashing.
    """
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, bool):  # bool is an int subclass — never a money value
        return None
    if isinstance(value, (int, float)):
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return result if result.is_finite() else None
    if isinstance(value, str):
        cleaned = value.strip().replace("€", "").replace("EUR", "").replace("%", "").strip()
        cleaned = cleaned.replace(" ", "")
        # German grouping/decimal: "1.234,56" -> "1234.56"; plain "1234.56" kept.
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            result = Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return None
        return result if result.is_finite() else None
    return None


def to_rate(value) -> Optional[Decimal]:
    """Normalise a VAT rate to a whole-percent Decimal (19, 7, 0).

    Accepts 19, "19", "19%", 0.19 (fraction) and collapses to percent points.
    Anything unrecognised is ``None`` (flagged downstream, never guessed).
    """
    number = to_decimal(value)
    if number is None:
        return None
    if number < 0:
        return None
    # A fraction like 0.19 means 19% — scale it up. Exactly 0 stays 0.
    if 0 < number < 1:
        number = number * Decimal("100")
    return number.quantize(Decimal("1")) if number == number.to_integral_value() else number


@dataclass(frozen=True)
class ExtractedLineItem:
    """One invoice position, before Kontierung.

    ``tax_rate`` is a percent (19, 7, 0). The booking engine turns
    (direction, tax_rate) into an SKR account + Steuerschlüssel; it lives on the
    persisted line item, never here.
    """

    description: str = ""
    quantity: Optional[Decimal] = None
    amount_net: Optional[Decimal] = None
    tax_rate: Optional[Decimal] = None
    amount_tax: Optional[Decimal] = None
    amount_gross: Optional[Decimal] = None


@dataclass(frozen=True)
class TaxBreakdownEntry:
    """Per-rate net/tax subtotal — the basis for the Rechenprobe and later UStVA."""

    rate: Decimal
    net: Decimal
    tax: Decimal


@dataclass(frozen=True)
class ExtractedInvoice:
    """Structured result of extracting one invoice image/PDF.

    Every field is optional/best-effort: extraction never blocks on a missing
    value, it records what it found and lets validation flag the gaps. Direction
    defaults to ``incoming`` (the common expense-processing case) and is corrected
    on confirm.
    """

    direction: str = INCOMING
    issuer_name: str = ""
    issuer_vat_id: str = ""
    issuer_tax_number: str = ""
    recipient_name: str = ""
    recipient_vat_id: str = ""
    invoice_number: str = ""
    invoice_date: Optional[date] = None
    service_date: Optional[date] = None
    currency: str = "EUR"
    line_items: tuple[ExtractedLineItem, ...] = ()
    tax_breakdown: tuple[TaxBreakdownEntry, ...] = ()
    total_net: Optional[Decimal] = None
    total_tax: Optional[Decimal] = None
    total_gross: Optional[Decimal] = None
    small_amount: bool = False
    reverse_charge: bool = False
    intra_community: bool = False
    payment_terms: str = ""
    confidence: Optional[Decimal] = None

    def normalized_direction(self) -> str:
        return self.direction if self.direction in (INCOMING, OUTGOING) else INCOMING
