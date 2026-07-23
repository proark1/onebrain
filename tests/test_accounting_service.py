"""Extraction service pipeline + the Drive malware-clean trigger hook."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.accounting.service as service_module
from app.accounting.base import accounting_category_id
from app.accounting.extraction import FakeInvoiceExtractor
from app.accounting.memory import MemoryAccountingStore
from app.accounting.model import ExtractedInvoice, ExtractedLineItem, TaxBreakdownEntry, INCOMING
from app.accounting.service import AccountingService
from app.jobs.base import JOB_ACCOUNTING_EXTRACT, Job
from app.platform.base import Account, AppInstallation, Space
from app.platform.memory import MemoryPlatformStore


def _clean_invoice() -> ExtractedInvoice:
    return ExtractedInvoice(
        direction=INCOMING, issuer_name="ACME GmbH", recipient_name="My Co",
        issuer_vat_id="DE123456789", invoice_number="R-2026-1", invoice_date=date(2026, 7, 1),
        line_items=(ExtractedLineItem(
            description="Consulting", amount_net=Decimal("1000.00"),
            tax_rate=Decimal("19"), amount_tax=Decimal("190.00"), amount_gross=Decimal("1190.00"),
        ),),
        tax_breakdown=(TaxBreakdownEntry(Decimal("19"), Decimal("1000.00"), Decimal("190.00")),),
        total_net=Decimal("1000.00"), total_tax=Decimal("190.00"), total_gross=Decimal("1190.00"),
        confidence=Decimal("0.92"),
    )


def _platform(*, install=True):
    platform = MemoryPlatformStore()
    platform.create_account(Account(id="acme", kind="organization", name="Acme", owner_user_id="admin@acme"))
    platform.create_space(Space(id="sp_business", account_id="acme", kind="business", name="Business"))
    if install:
        platform.install_app(AppInstallation(
            id="appi_buchhaltung", account_id="acme", app_id="buchhaltung",
            enabled_space_ids=("sp_business",),
            allowed_purposes=("accounting_read", "accounting_ingest", "accounting_configure"),
        ))
    return platform


class _FakeDriveStore:
    def __init__(self):
        self.files = {}
        self.revs = {}

    def add(self, file, revision):
        self.files[file.id] = file
        self.revs[revision.id] = revision

    def get_file(self, file_id, account_id, space_id):
        return self.files.get(file_id)

    def get_revision(self, revision_id, account_id, space_id):
        return self.revs.get(revision_id)

    def get_authoritative_malware_scan(self, revision_id, account_id, space_id):
        return SimpleNamespace(status="clean")


class _FakeBlob:
    def stat(self, storage_key):
        return SimpleNamespace()

    def iter_range(self, storage_key):
        yield b"imagebytes"


def _file(file_id="f1", revision_id="r1", *, category=None, generation=1, trashed_at=""):
    return SimpleNamespace(
        id=file_id, current_revision_id=revision_id, name="invoice.png",
        category=accounting_category_id("sp_business") if category is None else category,
        generation=generation, trashed_at=trashed_at,
    )


def _revision(revision_id="r1", file_id="f1"):
    return SimpleNamespace(
        id=revision_id, file_id=file_id, media_type="image/png",
        storage_key=f"blob/{revision_id}", size_bytes=10, sha256="deadbeef",
    )


def _job(file_id="f1", revision_id="r1"):
    return Job(
        id=f"job_{revision_id}", type=JOB_ACCOUNTING_EXTRACT, status="running",
        tenant_id="acme", account_id="acme", space_id="sp_business", requested_by="admin@acme",
        payload={"file_id": file_id, "revision_id": revision_id, "generation": 1},
    )


def _service(monkeypatch, platform, accounting, drive, *, extractor=None, clean=True):
    monkeypatch.setattr(service_module, "is_clean_attestation", lambda rev, scan: clean)
    monkeypatch.setattr(service_module, "blob_matches_revision", lambda info, size_bytes, sha256: True)
    return AccountingService(
        store=accounting, extractor=extractor or FakeInvoiceExtractor(_clean_invoice()),
        drive_store=drive, blob_store=_FakeBlob(), platform_store=platform, settings=SimpleNamespace(),
    )


def test_extraction_creates_a_prebooked_pending_draft(monkeypatch):
    platform, accounting = _platform(), MemoryAccountingStore()
    drive = _FakeDriveStore()
    drive.add(_file(), _revision())
    service = _service(monkeypatch, platform, accounting, drive)

    result = service.handle_extraction_job(_job())
    assert result["status"] == "extracted"

    document = accounting.get_document("acme", "sp_business", result["document_id"])
    assert document["status"] == "pending"
    assert document["direction"] == "incoming"
    assert document["total_gross"] == "1190.00"
    assert document["line_items"][0]["proposed_account"] == "4980"
    assert document["line_items"][0]["proposed_tax_key"] == "9"
    assert document["check_flags"]["needs_review"] is False  # clean invoice → batch-confirmable


def test_extraction_is_idempotent_per_revision(monkeypatch):
    platform, accounting = _platform(), MemoryAccountingStore()
    drive = _FakeDriveStore()
    drive.add(_file(), _revision())
    service = _service(monkeypatch, platform, accounting, drive)

    first = service.handle_extraction_job(_job())
    second = service.handle_extraction_job(_job())
    assert second["status"] == "exists"
    assert second["document_id"] == first["document_id"]
    assert len(accounting.list_documents("acme", "sp_business")) == 1


def test_extraction_skipped_when_module_not_installed(monkeypatch):
    platform, accounting = _platform(install=False), MemoryAccountingStore()
    drive = _FakeDriveStore()
    drive.add(_file(), _revision())
    service = _service(monkeypatch, platform, accounting, drive)
    assert service.handle_extraction_job(_job())["status"] == "not_installed"
    assert accounting.list_documents("acme", "sp_business") == []


def test_duplicate_invoice_is_flagged_not_dropped(monkeypatch):
    platform, accounting = _platform(), MemoryAccountingStore()
    drive = _FakeDriveStore()
    drive.add(_file("f1", "r1"), _revision("r1", "f1"))
    drive.add(_file("f2", "r2"), _revision("r2", "f2"))
    service = _service(monkeypatch, platform, accounting, drive)

    service.handle_extraction_job(_job("f1", "r1"))
    second = service.handle_extraction_job(_job("f2", "r2"))
    document = accounting.get_document("acme", "sp_business", second["document_id"])
    assert document["check_flags"]["duplicate"] is True
    assert document["check_flags"]["needs_review"] is True
    assert len(accounting.list_documents("acme", "sp_business")) == 2  # kept, not dropped


def test_quarantined_revision_is_a_no_op(monkeypatch):
    platform, accounting = _platform(), MemoryAccountingStore()
    drive = _FakeDriveStore()
    drive.add(_file(), _revision())
    service = _service(monkeypatch, platform, accounting, drive, clean=False)
    assert service.handle_extraction_job(_job())["status"] == "quarantined"
    assert accounting.list_documents("acme", "sp_business") == []


def test_stale_revision_is_a_no_op(monkeypatch):
    platform, accounting = _platform(), MemoryAccountingStore()
    drive = _FakeDriveStore()
    drive.add(_file("f1", "current"), _revision("r1", "f1"))  # file points at a newer revision
    service = _service(monkeypatch, platform, accounting, drive)
    assert service.handle_extraction_job(_job("f1", "r1"))["status"] == "stale"


def test_file_recategorised_after_enqueue_is_skipped(monkeypatch):
    platform, accounting = _platform(), MemoryAccountingStore()
    drive = _FakeDriveStore()
    drive.add(_file(category="general"), _revision())  # moved out of the accounting category
    service = _service(monkeypatch, platform, accounting, drive)
    assert service.handle_extraction_job(_job())["status"] == "decategorised"
    assert accounting.list_documents("acme", "sp_business") == []


def test_trashed_file_is_skipped(monkeypatch):
    platform, accounting = _platform(), MemoryAccountingStore()
    drive = _FakeDriveStore()
    drive.add(_file(trashed_at="2026-07-23T00:00:00Z"), _revision())
    service = _service(monkeypatch, platform, accounting, drive)
    assert service.handle_extraction_job(_job())["status"] == "trashed"


def test_unavailable_extractor_reports_status_without_crashing(monkeypatch):
    platform, accounting = _platform(), MemoryAccountingStore()
    drive = _FakeDriveStore()
    drive.add(_file(), _revision())
    extractor = FakeInvoiceExtractor(_clean_invoice(), available=False, unavailable_reason="no model")
    service = _service(monkeypatch, platform, accounting, drive, extractor=extractor)
    assert service.handle_extraction_job(_job())["status"] == "extractor_unavailable"


# ---- the Drive malware-clean trigger hook -----------------------------------

class _RecordingJobStore:
    def __init__(self):
        self.calls = []

    def enqueue(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id="job_enqueued")


def _hook_service(job_store):
    from app.drive.scanning import DriveMalwareScanningService

    service = DriveMalwareScanningService.__new__(DriveMalwareScanningService)
    service.job_store = job_store
    service.settings = SimpleNamespace(job_max_attempts=3)
    return service


def _completion(*, status="clean", category=None, space_id="sp_business"):
    file = SimpleNamespace(
        id="f1", current_revision_id="r1", generation=1, tenant_id="acme",
        account_id="acme", space_id=space_id,
        category=accounting_category_id(space_id) if category is None else category,
        uploaded_by="u1",
    )
    return SimpleNamespace(scan=SimpleNamespace(status=status), file=file)


def test_hook_enqueues_extraction_for_clean_buchhaltung_file():
    jobs = _RecordingJobStore()
    _hook_service(jobs)._enqueue_accounting_extraction_if_needed(_completion())
    assert len(jobs.calls) == 1
    assert jobs.calls[0]["type"] == JOB_ACCOUNTING_EXTRACT
    assert jobs.calls[0]["payload"]["file_id"] == "f1"


def test_hook_ignores_other_categories_and_unclean_files():
    jobs = _RecordingJobStore()
    service = _hook_service(jobs)
    service._enqueue_accounting_extraction_if_needed(_completion(category="general"))
    service._enqueue_accounting_extraction_if_needed(_completion(status="infected"))
    service._enqueue_accounting_extraction_if_needed(SimpleNamespace(scan=SimpleNamespace(status="clean"), file=None))
    assert jobs.calls == []
