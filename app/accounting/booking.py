"""Booking proposal (Kontierung) — rule-based v1, per line item.

The point of the module (plan §5): don't just capture fields, propose the
*booking* so the human confirms instead of types. This is a conservative
rule engine keyed on (direction, VAT rate) → SKR03 Sachkonto + DATEV
Steuerschlüssel, with a confidence that drops when the case is ambiguous
(0%/unknown rate, reverse-charge, innergemeinschaftlich) so those land in
single review rather than a batch confirm.

Every proposal is editable at confirmation (§2); corrections are what a later
learning classifier will train on. SKR03 only in v1 — SKR04 is a parallel table
to add later (plan §11); an unknown chart falls back to SKR03 conservatively.

DATEV Steuerschlüssel (BU-Schlüssel) used here:
  9 = Vorsteuer 19% · 8 = Vorsteuer 7% · 3 = Umsatzsteuer 19% · 2 = Umsatzsteuer 7%
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.accounting.model import ExtractedInvoice, INCOMING, OUTGOING


DEFAULT_CHART = "SKR03"

# SKR03 accounts. The incoming default is a generic operating-expense account
# (not "Wareneingang") because most captured invoices are services/overhead, not
# goods for resale — a safe "please classify" default the human refines. Revenue
# uses the automatic Erlöse accounts.
_SKR03 = {
    "expense": "4980",          # Sonstige betriebliche Aufwendungen
    "revenue_19": "8400",       # Erlöse 19% USt
    "revenue_7": "8300",        # Erlöse 7% USt
    "revenue_other": "8200",    # Erlöse (steuerfrei / other)
}

_RATE_19 = Decimal("19")
_RATE_7 = Decimal("7")


@dataclass(frozen=True)
class BookingProposal:
    account: str
    tax_key: str
    cost_center: str = ""
    confidence: Decimal = Decimal("0")


def _incoming(rate: Optional[Decimal]) -> BookingProposal:
    if rate == _RATE_19:
        return BookingProposal(_SKR03["expense"], "9", confidence=Decimal("0.90"))
    if rate == _RATE_7:
        return BookingProposal(_SKR03["expense"], "8", confidence=Decimal("0.90"))
    if rate == Decimal("0"):
        # steuerfrei / no input VAT — plausible but worth a look.
        return BookingProposal(_SKR03["expense"], "", confidence=Decimal("0.50"))
    # Unknown/None rate: still route to the expense account, but low confidence.
    return BookingProposal(_SKR03["expense"], "", confidence=Decimal("0.40"))


def _outgoing(rate: Optional[Decimal]) -> BookingProposal:
    if rate == _RATE_19:
        return BookingProposal(_SKR03["revenue_19"], "3", confidence=Decimal("0.90"))
    if rate == _RATE_7:
        return BookingProposal(_SKR03["revenue_7"], "2", confidence=Decimal("0.90"))
    if rate == Decimal("0"):
        return BookingProposal(_SKR03["revenue_other"], "", confidence=Decimal("0.50"))
    return BookingProposal(_SKR03["revenue_other"], "", confidence=Decimal("0.40"))


def propose_line(
    direction: str,
    tax_rate: Optional[Decimal],
    *,
    reverse_charge: bool = False,
    intra_community: bool = False,
    chart: str = DEFAULT_CHART,
) -> BookingProposal:
    """Propose account + Steuerschlüssel for one posting.

    Reverse-charge / innergemeinschaftlich cases (§13b, i.g. Erwerb) need special
    keys the v1 table does not model; we route to the base account with no tax key
    and a low confidence so the human always reviews and sets the correct key.
    """
    base = _incoming(tax_rate) if direction == INCOMING else _outgoing(tax_rate)
    if reverse_charge or intra_community:
        return BookingProposal(base.account, "", confidence=Decimal("0.30"))
    return base


def propose(invoice: ExtractedInvoice, *, chart: str = DEFAULT_CHART) -> tuple[BookingProposal, ...]:
    """One proposal per line item (mixed invoices split — never one summary row).

    When the extractor found no line items but has totals, synthesise a single
    posting from the document-level rate so the document is still bookable.
    """
    direction = invoice.normalized_direction()
    if invoice.line_items:
        return tuple(
            propose_line(
                direction,
                item.tax_rate,
                reverse_charge=invoice.reverse_charge,
                intra_community=invoice.intra_community,
                chart=chart,
            )
            for item in invoice.line_items
        )
    fallback_rate = invoice.tax_breakdown[0].rate if invoice.tax_breakdown else None
    return (
        propose_line(
            direction,
            fallback_rate,
            reverse_charge=invoice.reverse_charge,
            intra_community=invoice.intra_community,
            chart=chart,
        ),
    )
