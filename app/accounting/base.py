"""Accounting (Buchhaltung) domain constants and store contract.

Phase 0 is the module skeleton. The two RLS tables (``accounting_documents`` and
``accounting_line_items``) exist, but no ingest/extraction path writes to them yet
— that lands in Phase 1. The store therefore exposes only what the skeleton needs:
a per-workspace overview (counts) plus the GDPR export/erase scope operations that
every customer-data store must provide, so the module is compliant the moment
documents can be created.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


ACCOUNTING_APP_ID = "buchhaltung"
ACCOUNTING_READ_PURPOSE = "accounting_read"
ACCOUNTING_INGEST_PURPOSE = "accounting_ingest"
ACCOUNTING_CONFIGURE_PURPOSE = "accounting_configure"
ACCOUNTING_EXPORT_PURPOSE = "accounting_export"

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


class AccountingStore(Protocol):
    def overview(self, account_id: str, space_id: str) -> AccountingOverview: ...

    def export_scope(self, account_id: str, space_id: str = "") -> dict: ...

    def delete_scope(self, account_id: str, space_id: str = "") -> dict[str, int]: ...
