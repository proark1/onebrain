"""Invoice validation + duplicate signalling — pure functions over ExtractedInvoice.

Review-by-exception (plan §5) rests on this: a clean, non-duplicate invoice can be
batch-confirmed in one click, an flagged one is reviewed singly. NOTHING here drops
or auto-books a document — it only annotates ``check_flags`` and computes the
``dedup_key``. The service adds the store-dependent flags (duplicate, invoice-number
uniqueness) on top, because those need a scoped lookup.

Checks: Rechenprobe (net + tax = gross, per rate), §14 UStG mandatory-field
completeness (relaxed for a Kleinbetragsrechnung ≤250€, §33 UStDV), and USt-IdNr
shape. Money is Decimal; a small rounding tolerance absorbs per-rate cent rounding.
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from typing import Optional

from app.accounting.model import (
    ExtractedInvoice,
    SMALL_AMOUNT_GROSS_LIMIT,
    to_decimal,
)


_CENT = Decimal("0.01")
# Per-rate rounding can each drift a cent; allow a couple of cents on the total.
_ARITHMETIC_TOLERANCE = Decimal("0.02")

# EU USt-IdNr: 2-letter country + 2..12 alphanumerics. DE is DE + 9 digits.
_VAT_ID_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{2,12}$")
_DE_VAT_ID_RE = re.compile(r"^DE[0-9]{9}$")


def _q(value: Optional[Decimal]) -> Optional[Decimal]:
    return None if value is None else value.quantize(_CENT)


def _normalize_vat_id(raw: str) -> str:
    return re.sub(r"[\s.]", "", (raw or "")).upper()


def check_arithmetic(invoice: ExtractedInvoice) -> dict:
    """Rechenprobe: does net + tax = gross, and per-rate tax = net * rate?

    Returns ``{ok, delta, detail}``. ``ok`` is False only when we have enough
    numbers to prove a mismatch — a missing total is a completeness gap, not an
    arithmetic failure, so it never trips this on its own.
    """
    net = _q(invoice.total_net)
    tax = _q(invoice.total_tax)
    gross = _q(invoice.total_gross)

    detail: dict = {}
    checks: list[bool] = []

    if net is not None and tax is not None and gross is not None:
        delta = (net + tax - gross).copy_abs()
        detail["total_delta"] = str(delta)
        checks.append(delta <= _ARITHMETIC_TOLERANCE)

    # Per-rate: each breakdown line's tax should be net * rate%.
    breakdown_ok = True
    if invoice.tax_breakdown:
        sum_net = Decimal("0")
        sum_tax = Decimal("0")
        for entry in invoice.tax_breakdown:
            expected = (entry.net * entry.rate / Decimal("100")).quantize(_CENT)
            if (expected - _q(entry.tax)).copy_abs() > _ARITHMETIC_TOLERANCE:
                breakdown_ok = False
            sum_net += entry.net
            sum_tax += entry.tax
        if net is not None and (sum_net.quantize(_CENT) - net).copy_abs() > _ARITHMETIC_TOLERANCE:
            breakdown_ok = False
        if tax is not None and (sum_tax.quantize(_CENT) - tax).copy_abs() > _ARITHMETIC_TOLERANCE:
            breakdown_ok = False
        checks.append(breakdown_ok)
        detail["breakdown_ok"] = breakdown_ok

    ok = all(checks) if checks else False
    detail["evaluated"] = bool(checks)
    return {"ok": ok, "detail": detail}


def check_mandatory_fields(invoice: ExtractedInvoice) -> dict:
    """§14 UStG completeness over the fields we persist (relaxed for small amounts)."""
    gross = invoice.total_gross
    is_small = bool(invoice.small_amount) or (
        gross is not None and gross <= SMALL_AMOUNT_GROSS_LIMIT
    )

    missing: list[str] = []
    if not invoice.issuer_name.strip():
        missing.append("issuer_name")
    if invoice.invoice_date is None:
        missing.append("invoice_date")
    if invoice.total_gross is None:
        missing.append("total_gross")
    has_rate = bool(invoice.tax_breakdown) or any(
        item.tax_rate is not None for item in invoice.line_items
    )
    if not has_rate and invoice.total_tax is None:
        missing.append("tax_rate")

    if not is_small:
        # Full §14 set — a Kleinbetragsrechnung (§33 UStDV) waives recipient,
        # invoice number, and the separate net/tax split.
        if not invoice.recipient_name.strip():
            missing.append("recipient_name")
        if not invoice.invoice_number.strip():
            missing.append("invoice_number")
        if not (invoice.issuer_vat_id.strip() or invoice.issuer_tax_number.strip()):
            missing.append("issuer_tax_id")
        if invoice.total_net is None:
            missing.append("total_net")
        if not invoice.line_items:
            missing.append("line_items")

    return {"complete": not missing, "missing": missing, "small_amount": is_small}


def check_vat_id(invoice: ExtractedInvoice) -> Optional[bool]:
    """None when no USt-IdNr was found; else whether it is well-formed."""
    raw = _normalize_vat_id(invoice.issuer_vat_id)
    if not raw:
        return None
    if raw.startswith("DE"):
        return bool(_DE_VAT_ID_RE.fullmatch(raw))
    return bool(_VAT_ID_RE.fullmatch(raw))


def compute_dedup_key(invoice: ExtractedInvoice, *, file_sha256: str = "") -> str:
    """Fuzzy business key (issuer + invoice-number + gross + date).

    Same invoice arriving twice — a photo and the PDF, or an upload and a later
    e-mail (Phase 2) — collapses to the same key so the second is flagged, not
    silently re-booked. Falls back to the exact file hash when the business
    fields are too empty to key on; empty string means "cannot dedup" (matches
    the partial index ``WHERE dedup_key <> ''``).
    """
    issuer = re.sub(r"\s+", " ", invoice.issuer_name.strip().casefold())
    number = re.sub(r"\s+", "", invoice.invoice_number.strip().casefold())
    gross = str(_q(invoice.total_gross)) if invoice.total_gross is not None else ""
    date = invoice.invoice_date.isoformat() if invoice.invoice_date else ""
    if number or (issuer and gross):
        basis = "|".join((invoice.normalized_direction(), issuer, number, gross, date))
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:40]
    if file_sha256:
        return "file:" + file_sha256[:34]
    return ""


def validate(invoice: ExtractedInvoice, *, file_sha256: str = "") -> tuple[dict, str]:
    """Assemble ``check_flags`` and the dedup key for one extracted invoice.

    The returned flags always carry ``needs_review``; the service ORs in the
    store-derived ``duplicate`` / ``invoice_number_unique`` before persisting.
    """
    arithmetic = check_arithmetic(invoice)
    mandatory = check_mandatory_fields(invoice)
    vat_ok = check_vat_id(invoice)

    currency = (invoice.currency or "EUR").upper()
    flags = {
        "arithmetic_ok": arithmetic["ok"],
        "arithmetic": arithmetic["detail"],
        "mandatory_complete": mandatory["complete"],
        "missing_fields": mandatory["missing"],
        "small_amount": mandatory["small_amount"],
        "vat_id_valid": vat_ok,
        "reverse_charge": bool(invoice.reverse_charge),
        "intra_community": bool(invoice.intra_community),
        # Non-EUR invoices are captured but never folded into the EUR VAT dashboard
        # (this German-first path does no conversion); flag them for a human.
        "non_eur": currency != "EUR",
    }
    flags["needs_review"] = needs_review(flags)
    return flags, compute_dedup_key(invoice, file_sha256=file_sha256)


def needs_review(flags: dict) -> bool:
    """A flag set warrants single review (vs. batch confirm) if anything is off."""
    return bool(
        not flags.get("arithmetic_ok")
        or not flags.get("mandatory_complete")
        or flags.get("vat_id_valid") is False
        or flags.get("duplicate")
        or flags.get("invoice_number_unique") is False
        or flags.get("reverse_charge")
        or flags.get("intra_community")
        or flags.get("non_eur")
    )
