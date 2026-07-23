"""Accounting service — the extraction pipeline that composes the pieces.

One entry point runs for each ``accounting_extract`` job: verify the module is
installed, read the malware-clean Drive bytes (mirroring the Drive indexer's
gate), extract with the vision model, validate + dedup, propose the booking per
line, and persist a single ``pending`` draft. Nothing here books anything — a
human confirms (§2). Read/confirm live in the router; this is the write side the
background worker drives.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.accounting.base import (
    ACCOUNTING_APP_ID,
    ACCOUNTING_INGEST_PURPOSE,
    accounting_category_id,
    accounting_extract_enqueue_kwargs,
)
from app.accounting.booking import propose
from app.accounting.extraction import (
    InvoiceExtractionError,
    InvoiceExtractorUnavailable,
)
from app.accounting.model import ExtractedInvoice, ExtractedLineItem
from app.accounting.validation import validate
from app.drive.base import is_clean_attestation
from app.drive.blobs import blob_matches_revision


logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _money(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except Exception:
            return None
    return str(value.quantize(Decimal("0.01")))


def _rate(value) -> str | None:
    return None if value is None else str(value)


def _confidence(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except Exception:
            return None
    value = max(Decimal("0"), min(Decimal("1"), value))
    return str(value.quantize(Decimal("0.001")))


def _iso_date(value) -> str | None:
    return value.isoformat() if value else None


def build_document_row(
    document_id: str,
    account_id: str,
    space_id: str,
    *,
    invoice: ExtractedInvoice,
    flags: dict,
    dedup_key: str,
    drive_file_id: str,
    drive_revision_id: str,
    created_by: str,
    now: str,
) -> dict:
    breakdown = [
        {"rate": _rate(entry.rate), "net": _money(entry.net), "tax": _money(entry.tax)}
        for entry in invoice.tax_breakdown
    ]
    return {
        "id": document_id,
        # tenant_id == account_id on a customer box (the accounting store's scope).
        "tenant_id": account_id,
        "account_id": account_id,
        "space_id": space_id,
        "direction": invoice.normalized_direction(),
        "issuer_name": invoice.issuer_name,
        "recipient_name": invoice.recipient_name,
        "invoice_number": invoice.invoice_number,
        "invoice_date": _iso_date(invoice.invoice_date),
        "service_date": _iso_date(invoice.service_date),
        "currency": invoice.currency or "EUR",
        "total_net": _money(invoice.total_net),
        "total_tax": _money(invoice.total_tax),
        "total_gross": _money(invoice.total_gross),
        "tax_breakdown": breakdown,
        "dedup_key": dedup_key,
        "check_flags": flags,
        "status": "pending",
        "confidence": _confidence(invoice.confidence),
        "jurisdiction": "DE",
        "drive_file_id": drive_file_id,
        "drive_revision_id": drive_revision_id,
        "created_by": created_by,
        "confirmed_by": "",
        "created_at": now,
        "updated_at": now,
    }


def build_line_item_rows(
    document_id: str,
    account_id: str,
    space_id: str,
    *,
    invoice: ExtractedInvoice,
    now: str,
) -> list[dict]:
    items = invoice.line_items
    if not items:
        # No positions found — synthesise one from the totals so the document is
        # still bookable (mixed invoices keep their per-line splits otherwise).
        rate = invoice.tax_breakdown[0].rate if invoice.tax_breakdown else None
        items = (ExtractedLineItem(
            description="",
            amount_net=invoice.total_net,
            tax_rate=rate,
            amount_tax=invoice.total_tax,
            amount_gross=invoice.total_gross,
        ),)
    proposals = propose(invoice)
    rows: list[dict] = []
    for index, (item, proposal) in enumerate(zip(items, proposals)):
        rows.append({
            "id": f"acctli_{uuid4().hex}",
            "tenant_id": account_id,
            "account_id": account_id,
            "space_id": space_id,
            "document_id": document_id,
            "line_no": index,
            "description": item.description,
            "amount_net": _money(item.amount_net),
            "tax_rate": _rate(item.tax_rate),
            "amount_tax": _money(item.amount_tax),
            "amount_gross": _money(item.amount_gross),
            "proposed_account": proposal.account,
            "confirmed_account": "",
            "proposed_tax_key": proposal.tax_key,
            "confirmed_tax_key": "",
            "cost_center": proposal.cost_center,
            "created_at": now,
            "updated_at": now,
        })
    return rows


def reconcile_pending_extractions(
    *, drive_store, accounting_store, job_store, settings,
    account_id: str, space_id: str, limit: int = 100,
) -> int:
    """Re-enqueue extraction for malware-clean accounting files that have no document.

    The durability companion to the clean-scan trigger
    (``DriveMalwareScanningService._enqueue_accounting_extraction_if_needed``). If
    the worker crashes between committing the clean verdict and enqueuing
    extraction, the terminal-scan replay returns early and never re-enqueues, so
    the invoice is stranded: clean + in the accounting category, but no document.
    This re-derives that work from durable state, exactly as ``_reconcile_index_jobs``
    re-derives a lost Drive ingest from the persisted ``index_status='queued'``
    marker.

    The already-documented revisions are excluded *inside* the candidate query, so
    the ``limit`` bounds the set of files that still need work — an older stranded
    file is never hidden behind ``limit`` newer already-extracted ones. Idempotent:
    the shared idempotency key dedupes a racing retry so at most one extraction job
    exists per (file, revision, generation), and ``handle_extraction_job`` re-checks
    ``document_for_revision`` before persisting (covering the race where a document
    lands between the snapshot below and the enqueue). Tenant-scoped (one
    account+space) so it stays within RLS. Returns the count of clean accounting
    files still lacking a document that were enqueued this pass.
    """
    category = accounting_category_id(space_id)
    max_attempts = getattr(settings, "job_max_attempts", 3)
    documented = accounting_store.documented_revision_ids(account_id, space_id)
    enqueued = 0
    for file in drive_store.list_clean_category_files(
        account_id=account_id, space_id=space_id, category=category,
        exclude_revision_ids=documented, limit=limit,
    ):
        job_store.enqueue(**accounting_extract_enqueue_kwargs(file, max_attempts=max_attempts))
        enqueued += 1
    return enqueued


class AccountingService:
    def __init__(self, *, store, extractor, drive_store, blob_store, platform_store, settings):
        self.store = store
        self.extractor = extractor
        self.drive_store = drive_store
        self.blob_store = blob_store
        self.platform_store = platform_store
        self.settings = settings

    def handle_extraction_job(self, job) -> dict:
        """Run one accounting extraction (invoked by the job worker)."""
        account_id = job.account_id
        space_id = job.space_id
        file_id = str(job.payload.get("file_id") or "")
        revision_id = str(job.payload.get("revision_id") or "")
        if not (account_id and space_id and file_id and revision_id):
            raise ValueError("Accounting extraction job is missing file identity.")

        # The install gate is the module's on/off switch — never extract for a
        # space where Buchhaltung is not installed, even if a file is categorised.
        decision = self.platform_store.check_app_access(
            account_id, ACCOUNTING_APP_ID, space_id, ACCOUNTING_INGEST_PURPOSE,
        )
        if not decision.allowed:
            return {"status": "not_installed", "reason": decision.reason}

        existing = self.store.document_for_revision(account_id, space_id, file_id, revision_id)
        if existing:
            return {"status": "exists", "document_id": existing["id"]}

        generation = job.payload.get("generation")
        read = self._read_clean_bytes(account_id, space_id, file_id, revision_id, generation)
        if isinstance(read, dict):
            return read  # a stale/quarantined/mismatch/decategorised no-op status
        file, revision, data = read

        if not self.extractor.available:
            return {"status": "extractor_unavailable", "reason": self.extractor.unavailable_reason}
        try:
            invoice = self.extractor.extract(
                content=data, media_type=revision.media_type, filename=file.name,
            )
        except InvoiceExtractorUnavailable as exc:
            # A configuration gap (no vision / sovereign model) is visible, not a
            # crash-loop: succeed the job with a status; new uploads retry.
            return {"status": "extractor_unavailable", "reason": str(exc)}
        # InvoiceExtractionError (bad file / model failure) propagates → job retry.

        document = self._persist(
            account_id, space_id,
            invoice=invoice, drive_file_id=file.id,
            drive_revision_id=revision.id, revision_sha256=revision.sha256,
            created_by=job.requested_by,
        )
        return {
            "status": "extracted",
            "document_id": document["id"],
            "needs_review": document["check_flags"].get("needs_review", True),
        }

    def _read_clean_bytes(
        self, account_id: str, space_id: str, file_id: str, revision_id: str, generation=None,
    ):
        file = self.drive_store.get_file(file_id, account_id=account_id, space_id=space_id)
        if not file or file.current_revision_id != revision_id:
            return {"status": "stale", "file_id": file_id}
        if generation is not None and getattr(file, "generation", generation) != generation:
            return {"status": "stale", "file_id": file_id}
        # The file may have been re-categorised, moved out of the audience, or trashed
        # between the clean-scan enqueue and now — it is no longer an accounting doc.
        if file.category != accounting_category_id(space_id):
            return {"status": "decategorised", "file_id": file_id}
        if getattr(file, "trashed_at", ""):
            return {"status": "trashed", "file_id": file_id}
        revision = self.drive_store.get_revision(
            revision_id, account_id=account_id, space_id=space_id,
        )
        if not revision or revision.file_id != file.id:
            return {"status": "missing_revision", "file_id": file_id}
        scan = self.drive_store.get_authoritative_malware_scan(
            revision.id, account_id=account_id, space_id=space_id,
        )
        if not is_clean_attestation(revision, scan):
            return {"status": "quarantined", "file_id": file_id}
        info = self.blob_store.stat(revision.storage_key)
        if not blob_matches_revision(info, size_bytes=revision.size_bytes, sha256=revision.sha256):
            return {"status": "blob_mismatch", "file_id": file_id}
        data = b"".join(self.blob_store.iter_range(revision.storage_key))
        return file, revision, data

    def _persist(
        self, account_id: str, space_id: str, *,
        invoice: ExtractedInvoice, drive_file_id: str, drive_revision_id: str,
        revision_sha256: str, created_by: str,
    ) -> dict:
        flags, dedup_key = validate(invoice, file_sha256=revision_sha256)
        # Preserve the extracted legal identifiers/terms — 0036 has no header column
        # for them, but a reviewer must be able to verify what was read.
        flags["extracted_fields"] = {
            "issuer_vat_id": invoice.issuer_vat_id,
            "issuer_tax_number": invoice.issuer_tax_number,
            "recipient_vat_id": invoice.recipient_vat_id,
            "payment_terms": invoice.payment_terms,
        }
        # duplicate / invoice-number uniqueness are finalised by the store UNDER its
        # write lock (create_document), so concurrent duplicate uploads stay flagged.

        document_id = f"acctdoc_{uuid4().hex}"
        now = _now_iso()
        document = build_document_row(
            document_id, account_id, space_id,
            invoice=invoice, flags=flags, dedup_key=dedup_key,
            drive_file_id=drive_file_id, drive_revision_id=drive_revision_id,
            created_by=created_by, now=now,
        )
        line_items = build_line_item_rows(
            document_id, account_id, space_id, invoice=invoice, now=now,
        )
        return self.store.create_document(document, line_items)
