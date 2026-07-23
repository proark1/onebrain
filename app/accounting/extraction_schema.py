"""The §14 UStG extraction contract: JSON schema, prompt, and JSON → model parse.

Kept separate from the extractor wiring so the schema/prompt can be reviewed as a
domain artefact and the parse is unit-testable without any model. Numbers are
requested as decimal *strings* (dot separator) so a locale comma or a float can't
silently corrupt a cent; ``model.to_decimal`` re-normalises defensively anyway.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from app.accounting.model import (
    ExtractedInvoice,
    ExtractedLineItem,
    TaxBreakdownEntry,
    INCOMING,
    OUTGOING,
    to_decimal,
    to_rate,
)


# LiteLLM forwards this verbatim as response_format.json_schema. strict is False:
# a vision model reading a photographed invoice cannot guarantee every field, and
# an over-strict schema makes providers refuse rather than return partial truth —
# validation (not the schema) is where completeness is judged.
INVOICE_JSON_SCHEMA: dict = {
    "name": "invoice_extraction",
    "strict": False,
    "schema": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": [INCOMING, OUTGOING],
                "description": "incoming = a bill we received (expense); outgoing = one we issued (revenue).",
            },
            "issuer_name": {"type": "string", "description": "Supplier / Aussteller full name."},
            "issuer_vat_id": {"type": "string", "description": "Issuer USt-IdNr, e.g. DE123456789."},
            "issuer_tax_number": {"type": "string", "description": "Issuer Steuernummer if no USt-IdNr."},
            "recipient_name": {"type": "string", "description": "Bill recipient / Empfänger full name."},
            "recipient_vat_id": {"type": "string"},
            "invoice_number": {"type": "string", "description": "Rechnungsnummer."},
            "invoice_date": {"type": "string", "description": "Ausstellungsdatum as YYYY-MM-DD."},
            "service_date": {"type": "string", "description": "Leistungsdatum as YYYY-MM-DD."},
            "currency": {"type": "string", "description": "ISO currency, default EUR."},
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "quantity": {"type": "string"},
                        "amount_net": {"type": "string"},
                        "tax_rate": {"type": "string", "description": "Percent, e.g. 19."},
                        "amount_tax": {"type": "string"},
                        "amount_gross": {"type": "string"},
                    },
                },
            },
            "tax_breakdown": {
                "type": "array",
                "description": "Net + tax subtotal per VAT rate.",
                "items": {
                    "type": "object",
                    "properties": {
                        "rate": {"type": "string"},
                        "net": {"type": "string"},
                        "tax": {"type": "string"},
                    },
                },
            },
            "total_net": {"type": "string"},
            "total_tax": {"type": "string"},
            "total_gross": {"type": "string"},
            "small_amount": {"type": "boolean", "description": "Kleinbetragsrechnung ≤250€ gross."},
            "reverse_charge": {"type": "boolean", "description": "Steuerschuldnerschaft des Leistungsempfängers (§13b)."},
            "intra_community": {"type": "boolean", "description": "Innergemeinschaftliche Lieferung/Leistung."},
            "payment_terms": {"type": "string", "description": "Zahlungsziel / Skonto text."},
            "confidence": {"type": "number", "description": "0..1 self-assessed extraction confidence."},
        },
    },
}


EXTRACTION_SYSTEM_PROMPT = (
    "You are a meticulous German accounting clerk (Buchhalter) extracting the "
    "legally required fields (§14 UStG) from an invoice image. Read only what is "
    "printed; never invent a value. Return every monetary amount as a decimal "
    "string with a dot separator and no currency symbol (e.g. \"1234.56\"). Return "
    "dates as YYYY-MM-DD. Percentages as plain numbers (19, 7, 0). If a field is "
    "absent, return an empty string (or omit it) rather than guessing. Respond with "
    "the JSON object only."
)

EXTRACTION_USER_PROMPT = (
    "Extract this invoice into the required JSON. Determine the direction (incoming "
    "expense vs. outgoing revenue), the parties and their tax identifiers, the "
    "invoice number and dates, the line items, the net/tax/gross totals broken down "
    "per VAT rate, and flag Kleinbetragsrechnung, Reverse-Charge and "
    "innergemeinschaftlich where applicable."
)


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "ja", "1"}
    return bool(value)


def _line_items(raw) -> tuple[ExtractedLineItem, ...]:
    items: list[ExtractedLineItem] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        items.append(ExtractedLineItem(
            description=str(entry.get("description", "") or "").strip(),
            quantity=to_decimal(entry.get("quantity")),
            amount_net=to_decimal(entry.get("amount_net")),
            tax_rate=to_rate(entry.get("tax_rate")),
            amount_tax=to_decimal(entry.get("amount_tax")),
            amount_gross=to_decimal(entry.get("amount_gross")),
        ))
    return tuple(items)


def _tax_breakdown(raw) -> tuple[TaxBreakdownEntry, ...]:
    entries: list[TaxBreakdownEntry] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        rate = to_rate(entry.get("rate"))
        net = to_decimal(entry.get("net"))
        tax = to_decimal(entry.get("tax"))
        if rate is None or net is None or tax is None:
            continue  # only complete rate/net/tax rows feed the Rechenprobe
        entries.append(TaxBreakdownEntry(rate=rate, net=net, tax=tax))
    return tuple(entries)


def parse_invoice_json(payload) -> ExtractedInvoice:
    """Map a raw model JSON object to an ExtractedInvoice, tolerating gaps."""
    if not isinstance(payload, dict):
        raise ValueError("Invoice extraction result was not a JSON object.")
    direction = str(payload.get("direction", "") or "").strip().lower()
    currency = str(payload.get("currency", "") or "").strip().upper() or "EUR"
    return ExtractedInvoice(
        direction=direction if direction in (INCOMING, OUTGOING) else INCOMING,
        issuer_name=str(payload.get("issuer_name", "") or "").strip(),
        issuer_vat_id=str(payload.get("issuer_vat_id", "") or "").strip(),
        issuer_tax_number=str(payload.get("issuer_tax_number", "") or "").strip(),
        recipient_name=str(payload.get("recipient_name", "") or "").strip(),
        recipient_vat_id=str(payload.get("recipient_vat_id", "") or "").strip(),
        invoice_number=str(payload.get("invoice_number", "") or "").strip(),
        invoice_date=_parse_date(payload.get("invoice_date")),
        service_date=_parse_date(payload.get("service_date")),
        currency=currency,
        line_items=_line_items(payload.get("line_items")),
        tax_breakdown=_tax_breakdown(payload.get("tax_breakdown")),
        total_net=to_decimal(payload.get("total_net")),
        total_tax=to_decimal(payload.get("total_tax")),
        total_gross=to_decimal(payload.get("total_gross")),
        small_amount=_bool(payload.get("small_amount")),
        reverse_charge=_bool(payload.get("reverse_charge")),
        intra_community=_bool(payload.get("intra_community")),
        payment_terms=str(payload.get("payment_terms", "") or "").strip(),
        confidence=to_decimal(payload.get("confidence")),
    )
