"""Thread-safe JSON-backed accounting store for local development and tests.

Phase 1 adds the write path: extraction creates a ``pending`` document + line
items, a human confirms it (batch or single) to ``confirmed``, and only confirmed
documents fold into the summary. Money is stored as decimal *strings* (JSON-safe,
mirroring the Postgres store's ``_json_safe`` output) and parsed back to Decimal
for aggregation. Every read is filtered by ``account_id`` + ``space_id`` — the
in-memory analogue of the RLS scope the Postgres store enforces.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from app.accounting.base import AccountingOverview, build_summary
from app.accounting.model import INCOMING, OUTGOING
from app.accounting.validation import needs_review


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dec(value) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def summarize_documents(account_id: str, space_id: str, documents: list[dict]) -> dict:
    """Counts + confirmed-only per-direction money for one workspace."""
    total = len(documents)
    pending = sum(1 for row in documents if row.get("status") == "pending")
    confirmed = [row for row in documents if row.get("status") == "confirmed"]

    def side(direction: str) -> dict:
        # Only EUR money aggregates — non-EUR invoices are captured + flagged, never
        # summed into the EUR VAT dashboard (no conversion on this German-first path).
        rows = [
            row for row in confirmed
            if row.get("direction") == direction and (row.get("currency") or "EUR").upper() == "EUR"
        ]
        return {
            "count": len(rows),
            "net": sum((_dec(row.get("total_net")) for row in rows), Decimal("0")),
            "tax": sum((_dec(row.get("total_tax")) for row in rows), Decimal("0")),
            "gross": sum((_dec(row.get("total_gross")) for row in rows), Decimal("0")),
        }

    return build_summary(
        account_id, space_id,
        total=total, pending=pending, confirmed=len(confirmed),
        incoming=side(INCOMING), outgoing=side(OUTGOING),
    )


class MemoryAccountingStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._documents: dict[str, dict] = {}
        self._line_items: dict[str, dict] = {}
        self._persist_path = persist_path
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not (self._persist_path and os.path.exists(self._persist_path)):
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self._documents = {row["id"]: row for row in data.get("documents", [])}
            self._line_items = {row["id"]: row for row in data.get("line_items", [])}
        except Exception:
            # Accounting persistence is its own failure domain. Never touch platform data.
            self._documents = {}
            self._line_items = {}

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        with open(self._persist_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "documents": list(self._documents.values()),
                    "line_items": list(self._line_items.values()),
                },
                handle,
            )

    # ---- reads --------------------------------------------------------------

    def _scoped(self, account_id: str, space_id: str) -> list[dict]:
        return [
            row for row in self._documents.values()
            if row.get("account_id") == account_id and row.get("space_id") == space_id
        ]

    def _hydrate(self, document: dict) -> dict:
        lines = [
            dict(row) for row in self._line_items.values()
            if row.get("document_id") == document.get("id")
        ]
        lines.sort(key=lambda row: (row.get("line_no", 0), row.get("id", "")))
        return {**document, "line_items": lines}

    def overview(self, account_id: str, space_id: str) -> AccountingOverview:
        documents = self._scoped(account_id, space_id)
        pending = sum(1 for row in documents if row.get("status") == "pending")
        confirmed = sum(1 for row in documents if row.get("status") == "confirmed")
        return AccountingOverview(
            account_id=account_id,
            space_id=space_id,
            total_documents=len(documents),
            pending_documents=pending,
            confirmed_documents=confirmed,
        )

    def summary(self, account_id: str, space_id: str) -> dict:
        return summarize_documents(account_id, space_id, self._scoped(account_id, space_id))

    def get_document(self, account_id: str, space_id: str, document_id: str) -> Optional[dict]:
        row = self._documents.get(document_id)
        if not row or row.get("account_id") != account_id or row.get("space_id") != space_id:
            return None
        return self._hydrate(row)

    def list_documents(self, account_id: str, space_id: str, status: str = "") -> list[dict]:
        rows = self._scoped(account_id, space_id)
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows.sort(key=lambda row: (row.get("created_at", ""), row.get("id", "")), reverse=True)
        return [self._hydrate(row) for row in rows]

    def find_duplicate(
        self, account_id: str, space_id: str, dedup_key: str, *, exclude_id: str = "",
    ) -> Optional[dict]:
        if not dedup_key:
            return None
        for row in self._scoped(account_id, space_id):
            if row.get("dedup_key") == dedup_key and row.get("id") != exclude_id:
                return self._hydrate(row)
        return None

    def document_for_revision(
        self, account_id: str, space_id: str, drive_file_id: str, drive_revision_id: str,
    ) -> Optional[dict]:
        for row in self._scoped(account_id, space_id):
            if (
                row.get("drive_file_id") == drive_file_id
                and row.get("drive_revision_id") == drive_revision_id
            ):
                return self._hydrate(row)
        return None

    def documented_revision_ids(self, account_id: str, space_id: str) -> set[str]:
        """Revisions in this workspace that already have a document (extraction done)."""
        return {
            row["drive_revision_id"]
            for row in self._scoped(account_id, space_id)
            if row.get("drive_revision_id")
        }

    def invoice_number_seen(
        self, account_id: str, space_id: str, issuer_name: str, invoice_number: str,
        *, exclude_id: str = "",
    ) -> bool:
        number = (invoice_number or "").strip().casefold()
        if not number:
            return False
        issuer = (issuer_name or "").strip().casefold()
        for row in self._scoped(account_id, space_id):
            if row.get("id") == exclude_id:
                continue
            if (
                (row.get("invoice_number", "") or "").strip().casefold() == number
                and (row.get("issuer_name", "") or "").strip().casefold() == issuer
            ):
                return True
        return False

    # ---- writes -------------------------------------------------------------

    def create_document(self, document: dict, line_items: list[dict]) -> dict:
        with self._lock:
            document_id = document["id"]
            if document_id in self._documents:
                return self._hydrate(self._documents[document_id])
            # Finalise duplicate / invoice-number flags UNDER the lock so two
            # concurrent creates of the same invoice cannot both see "no duplicate".
            account_id = document.get("account_id", "")
            space_id = document.get("space_id", "")
            flags = dict(document.get("check_flags") or {})
            duplicate = self.find_duplicate(
                account_id, space_id, document.get("dedup_key") or "", exclude_id=document_id,
            )
            flags["duplicate"] = bool(duplicate)
            flags["duplicate_of"] = duplicate["id"] if duplicate else ""
            flags["invoice_number_unique"] = not self.invoice_number_seen(
                account_id, space_id, document.get("issuer_name", ""),
                document.get("invoice_number", ""), exclude_id=document_id,
            )
            flags["needs_review"] = needs_review(flags)
            stored = {**document, "check_flags": flags}
            self._documents[document_id] = stored
            for line in line_items:
                self._line_items[line["id"]] = dict(line)
            self._save()
            return self._hydrate(stored)

    def confirm_documents(
        self, account_id: str, space_id: str, confirmations: list[dict], confirmed_by: str,
    ) -> list[dict]:
        updated: list[dict] = []
        with self._lock:
            timestamp = _now_iso()
            for confirmation in confirmations:
                document_id = confirmation.get("document_id", "")
                row = self._documents.get(document_id)
                if not row or row.get("account_id") != account_id or row.get("space_id") != space_id:
                    raise KeyError(document_id)
                corrections = {
                    correction["id"]: correction
                    for correction in confirmation.get("line_items", [])
                    if correction.get("id")
                }
                for line in self._line_items.values():
                    if line.get("document_id") != document_id:
                        continue
                    correction = corrections.get(line.get("id"), {})
                    # An explicit "" clears the field (e.g. a tax-exempt/reverse-charge
                    # line with no BU key); only an OMITTED field falls back to proposed.
                    if "account" in correction:
                        line["confirmed_account"] = (correction.get("account") or "").strip()
                    else:
                        line["confirmed_account"] = (line.get("proposed_account") or "").strip()
                    if "tax_key" in correction:
                        line["confirmed_tax_key"] = (correction.get("tax_key") or "").strip()
                    else:
                        line["confirmed_tax_key"] = (line.get("proposed_tax_key") or "").strip()
                    if "cost_center" in correction:
                        line["cost_center"] = (correction.get("cost_center") or "").strip()
                    line["updated_at"] = timestamp
                direction = confirmation.get("direction")
                if direction in (INCOMING, OUTGOING):
                    row["direction"] = direction
                row["status"] = "confirmed"
                row["confirmed_by"] = confirmed_by
                row["updated_at"] = timestamp
                updated.append(self._hydrate(row))
            self._save()
        return updated

    def export_scope(self, account_id: str, space_id: str = "") -> dict:
        documents = [
            row for row in self._documents.values()
            if row.get("account_id") == account_id and (not space_id or row.get("space_id") == space_id)
        ]
        line_items = [
            row for row in self._line_items.values()
            if row.get("account_id") == account_id and (not space_id or row.get("space_id") == space_id)
        ]
        return {"documents": documents, "line_items": line_items}
