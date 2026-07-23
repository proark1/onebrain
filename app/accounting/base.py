"""Accounting (Buchhaltung) domain constants and store contract.

Phase 0 is the module skeleton. The two RLS tables (``accounting_documents`` and
``accounting_line_items``) exist, but no ingest/extraction path writes to them yet
— that lands in Phase 1. The store therefore exposes only what the skeleton needs:
a per-workspace overview (counts) and a GDPR export scope.

Erasure is deliberately NOT wired here. Invoices are GoBD-retained, so a blanket
GDPR erase must not destroy them; accounting erasure runs through the retention /
legal-hold model (plan §7) that arrives with the write path in Phase 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Protocol


ACCOUNTING_APP_ID = "buchhaltung"
ACCOUNTING_READ_PURPOSE = "accounting_read"
ACCOUNTING_INGEST_PURPOSE = "accounting_ingest"
ACCOUNTING_CONFIGURE_PURPOSE = "accounting_configure"
ACCOUNTING_EXPORT_PURPOSE = "accounting_export"


def accounting_category_id(space_id: str) -> str:
    """Deterministic id of the ``buchhaltung`` Drive AccessGroup for a space.

    The Drive ``category`` on a file is an AccessGroup id (a confidential audience
    compartment), and AccessGroup ids are globally unique — so the accounting
    category can't be a bare literal. Both the install bootstrap (which seeds the
    group) and the malware-clean scan trigger (which recognises "this file is an
    invoice") derive the same id from the space, so they always agree.
    """
    return f"acg_{space_id}_buchhaltung"

# Booking is always human-confirmed: extraction creates a ``pending`` draft and
# only a human confirmation makes it ``confirmed`` and countable in the overview.
DOCUMENT_STATUSES = frozenset({"pending", "confirmed"})
DOCUMENT_DIRECTIONS = frozenset({"incoming", "outgoing"})


@dataclass(frozen=True)
class AccountingOverview:
    """Aggregate document counts for one workspace.

    Only ``confirmed`` documents are booked. ``pending`` drafts are surfaced
    separately and never fold into a total/VAT figure until a human confirms.
    """

    account_id: str
    space_id: str
    total_documents: int = 0
    pending_documents: int = 0
    confirmed_documents: int = 0


_MONEY = Decimal("0.01")


def money_str(value) -> str:
    """Render a money value as a 2dp string (the JSON-safe wire form)."""
    if value in (None, ""):
        value = Decimal("0")
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except Exception:
            value = Decimal("0")
    return str(value.quantize(_MONEY))


def build_summary(
    account_id: str,
    space_id: str,
    *,
    total: int,
    pending: int,
    confirmed: int,
    incoming: dict,
    outgoing: dict,
) -> dict:
    """Shape the dashboard payload from already-aggregated figures.

    Memory sums in Python and Postgres sums in SQL, but both hand this the same
    per-direction ``{count, net, tax, gross}`` (confirmed only) so the wire shape
    is identical. Vorsteuer = input VAT on incoming; Umsatzsteuer = output VAT on
    outgoing; the balance is what would (Soll-versteuert) be owed.
    """
    input_vat = incoming.get("tax") or Decimal("0")
    output_vat = outgoing.get("tax") or Decimal("0")

    def _side(side: dict) -> dict:
        return {
            "count": int(side.get("count", 0)),
            "net": money_str(side.get("net")),
            "tax": money_str(side.get("tax")),
            "gross": money_str(side.get("gross")),
        }

    return {
        "account_id": account_id,
        "space_id": space_id,
        "currency": "EUR",
        "total_documents": total,
        "pending_documents": pending,
        "confirmed_documents": confirmed,
        "incoming": _side(incoming),
        "outgoing": _side(outgoing),
        "input_vat": money_str(input_vat),
        "output_vat": money_str(output_vat),
        "vat_balance": money_str(output_vat - input_vat),
    }


# A document dict carries exactly the ``accounting_documents`` columns; a hydrated
# document additionally carries a ``line_items`` list (each an
# ``accounting_line_items`` row). The service builds these dicts keyed by column
# name; both stores persist/return them unchanged so memory and Postgres stay
# symmetric (the same shape ``export_scope`` already returns).
class AccountingStore(Protocol):
    def overview(self, account_id: str, space_id: str) -> AccountingOverview: ...

    def summary(self, account_id: str, space_id: str) -> dict: ...

    def export_scope(self, account_id: str, space_id: str = "") -> dict: ...

    def create_document(self, document: dict, line_items: list[dict]) -> dict: ...

    def get_document(self, account_id: str, space_id: str, document_id: str) -> Optional[dict]: ...

    def list_documents(self, account_id: str, space_id: str, status: str = "") -> list[dict]: ...

    def find_duplicate(
        self, account_id: str, space_id: str, dedup_key: str, *, exclude_id: str = "",
    ) -> Optional[dict]: ...

    def document_for_revision(
        self, account_id: str, space_id: str, drive_file_id: str, drive_revision_id: str,
    ) -> Optional[dict]: ...

    def invoice_number_seen(
        self, account_id: str, space_id: str, issuer_name: str, invoice_number: str,
        *, exclude_id: str = "",
    ) -> bool: ...

    def confirm_documents(
        self, account_id: str, space_id: str, confirmations: list[dict], confirmed_by: str,
    ) -> list[dict]: ...
