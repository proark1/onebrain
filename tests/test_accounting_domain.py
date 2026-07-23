"""Pure-domain contracts: model parsing, Rechenprobe/§14 validation, SKR03 booking."""

from datetime import date
from decimal import Decimal

from app.accounting.booking import DEFAULT_CHART, propose, propose_line
from app.accounting.extraction_schema import parse_invoice_json
from app.accounting.model import (
    ExtractedInvoice,
    ExtractedLineItem,
    TaxBreakdownEntry,
    INCOMING,
    OUTGOING,
    to_decimal,
    to_rate,
)
from app.accounting.validation import compute_dedup_key, needs_review, validate


def _clean_invoice(**overrides) -> ExtractedInvoice:
    base = dict(
        direction=INCOMING,
        issuer_name="ACME GmbH",
        recipient_name="My Company",
        issuer_vat_id="DE123456789",
        invoice_number="R-2026-4711",
        invoice_date=date(2026, 7, 1),
        line_items=(ExtractedLineItem(
            description="Consulting", amount_net=Decimal("1000.00"),
            tax_rate=Decimal("19"), amount_tax=Decimal("190.00"),
            amount_gross=Decimal("1190.00"),
        ),),
        tax_breakdown=(TaxBreakdownEntry(Decimal("19"), Decimal("1000.00"), Decimal("190.00")),),
        total_net=Decimal("1000.00"),
        total_tax=Decimal("190.00"),
        total_gross=Decimal("1190.00"),
        confidence=Decimal("0.92"),
    )
    base.update(overrides)
    return ExtractedInvoice(**base)


# ---- number/rate coercion ---------------------------------------------------

def test_to_decimal_normalizes_locale_and_symbols():
    assert to_decimal("1.234,56") == Decimal("1234.56")
    assert to_decimal("1234.56") == Decimal("1234.56")
    assert to_decimal("€ 19,00") == Decimal("19.00")
    assert to_decimal("") is None
    assert to_decimal(None) is None
    assert to_decimal(True) is None  # bool is not a money value


def test_to_rate_handles_fraction_and_percent_sign():
    assert to_rate("19") == Decimal("19")
    assert to_rate("19%") == Decimal("19")
    assert to_rate(0.19) == Decimal("19")
    assert to_rate("0") == Decimal("0")
    assert to_rate("garbage") is None


# ---- parse ------------------------------------------------------------------

def test_parse_invoice_json_maps_strings_dates_and_breakdown():
    invoice = parse_invoice_json({
        "direction": "outgoing",
        "issuer_name": "Seller AG",
        "invoice_number": "2026-77",
        "invoice_date": "01.07.2026",   # German format tolerated
        "currency": "eur",
        "line_items": [
            {"description": "Item", "amount_net": "100,00", "tax_rate": "19%", "amount_tax": "19,00"},
        ],
        "tax_breakdown": [{"rate": "19", "net": "100.00", "tax": "19.00"}],
        "total_net": "100.00", "total_tax": "19.00", "total_gross": "119.00",
        "small_amount": True, "confidence": 0.8,
    })
    assert invoice.direction == OUTGOING
    assert invoice.invoice_date == date(2026, 7, 1)
    assert invoice.currency == "EUR"
    assert invoice.line_items[0].amount_net == Decimal("100.00")
    assert invoice.line_items[0].tax_rate == Decimal("19")
    assert invoice.tax_breakdown[0].tax == Decimal("19.00")
    assert invoice.total_gross == Decimal("119.00")
    assert invoice.small_amount is True


def test_parse_drops_incomplete_breakdown_rows():
    invoice = parse_invoice_json({"tax_breakdown": [{"rate": "19", "net": "100"}]})  # no tax
    assert invoice.tax_breakdown == ()


# ---- validation -------------------------------------------------------------

def test_clean_invoice_passes_and_is_not_flagged():
    flags, dedup = validate(_clean_invoice(), file_sha256="abc")
    assert flags["arithmetic_ok"] is True
    assert flags["mandatory_complete"] is True
    assert flags["vat_id_valid"] is True
    assert flags["needs_review"] is False
    assert dedup


def test_rechenprobe_catches_a_wrong_total():
    flags, _ = validate(_clean_invoice(total_gross=Decimal("2000.00")))
    assert flags["arithmetic_ok"] is False
    assert flags["needs_review"] is True


def test_full_invoice_missing_recipient_is_incomplete():
    # >250€ gross → full §14 set required, so a missing recipient is flagged.
    flags, _ = validate(_clean_invoice(recipient_name=""))
    assert flags["mandatory_complete"] is False
    assert "recipient_name" in flags["missing_fields"]


def test_kleinbetrag_relaxes_mandatory_fields():
    small = _clean_invoice(
        recipient_name="", invoice_number="",
        line_items=(ExtractedLineItem(amount_net=Decimal("40.00"), tax_rate=Decimal("19"), amount_tax=Decimal("7.60")),),
        tax_breakdown=(TaxBreakdownEntry(Decimal("19"), Decimal("40.00"), Decimal("7.60")),),
        total_net=Decimal("40.00"), total_tax=Decimal("7.60"), total_gross=Decimal("47.60"),
    )
    flags, _ = validate(small)
    assert flags["small_amount"] is True
    assert flags["mandatory_complete"] is True  # recipient/number waived under §33 UStDV


def test_bad_vat_id_is_flagged_but_absent_is_not():
    assert validate(_clean_invoice(issuer_vat_id="DE12"))[0]["vat_id_valid"] is False
    assert validate(_clean_invoice(issuer_vat_id=""))[0]["vat_id_valid"] is None


def test_non_eur_invoice_is_flagged_for_review():
    flags, _ = validate(_clean_invoice(currency="USD"))
    assert flags["non_eur"] is True
    assert flags["needs_review"] is True


def test_dedup_key_is_stable_for_the_same_invoice():
    a = compute_dedup_key(_clean_invoice(), file_sha256="x")
    b = compute_dedup_key(_clean_invoice(), file_sha256="y")
    assert a == b and a  # keyed on business fields, not the file hash
    other = compute_dedup_key(_clean_invoice(invoice_number="R-DIFFERENT"))
    assert other != a


def test_needs_review_reacts_to_store_derived_flags():
    flags, _ = validate(_clean_invoice())
    assert needs_review(flags) is False
    flags["duplicate"] = True
    assert needs_review(flags) is True


# ---- booking ----------------------------------------------------------------

def test_incoming_rates_map_to_skr03_vorsteuer_keys():
    assert propose_line(INCOMING, Decimal("19")).tax_key == "9"
    assert propose_line(INCOMING, Decimal("7")).tax_key == "8"
    assert propose_line(INCOMING, Decimal("19")).account == "4980"


def test_outgoing_rates_map_to_skr03_revenue_accounts():
    p19 = propose_line(OUTGOING, Decimal("19"))
    assert (p19.account, p19.tax_key) == ("8400", "3")
    p7 = propose_line(OUTGOING, Decimal("7"))
    assert (p7.account, p7.tax_key) == ("8300", "2")


def test_reverse_charge_forces_low_confidence_review():
    proposal = propose_line(INCOMING, Decimal("19"), reverse_charge=True)
    assert proposal.tax_key == ""
    assert proposal.confidence <= Decimal("0.3")


def test_propose_returns_one_proposal_per_line_item():
    invoice = _clean_invoice(line_items=(
        ExtractedLineItem(amount_net=Decimal("100"), tax_rate=Decimal("19")),
        ExtractedLineItem(amount_net=Decimal("50"), tax_rate=Decimal("7")),
    ))
    proposals = propose(invoice, chart=DEFAULT_CHART)
    assert [p.tax_key for p in proposals] == ["9", "8"]
