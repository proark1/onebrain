"""Durability of the accounting extraction enqueue.

The clean-scan trigger enqueues extraction as an in-memory side effect *after*
``complete_malware_scan`` commits the verdict. If the worker dies in that window
the verdict is durable but the enqueue is lost, and the terminal-scan replay
returns early without re-enqueuing — so a malware-clean invoice would never be
extracted. ``reconcile_pending_extractions`` closes that window by re-deriving
the work from durable state (clean verdict + accounting category + no document),
exactly as the Drive index reconcile re-derives a lost ingest. These tests drive
the crash, prove the replay does NOT self-heal, and prove the reconcile does.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.routers.accounting as accounting_router
from app.accounting.base import accounting_category_id, accounting_extract_idempotency_key
from app.accounting.extraction import FakeInvoiceExtractor
from app.accounting.memory import MemoryAccountingStore
from app.accounting.model import ExtractedInvoice, ExtractedLineItem, TaxBreakdownEntry
from app.accounting.service import AccountingService, reconcile_pending_extractions
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.drive.blobs import LocalDriveBlobStore
from app.drive.malware.fake import FakeMalwareScanner
from app.drive.memory import MemoryDriveStore
from app.drive.scanning import DriveMalwareScanningService
from app.drive.service import DriveService
from app.jobs.base import JOB_ACCOUNTING_EXTRACT, JOB_DRIVE_REVISION_MALWARE_SCAN
from app.jobs.memory import MemoryJobStore
from app.platform.base import Account, AppInstallation, Space
from app.platform.memory import MemoryPlatformStore
from app.store.memory import MemoryStore


ACCOUNT = "acme"
SPACE = "sp_business"
OWNER = "admin@acme"
ACCOUNTING_CATEGORY = accounting_category_id(SPACE)


def _sample_invoice() -> ExtractedInvoice:
    return ExtractedInvoice(
        direction="incoming", issuer_name="ACME GmbH", recipient_name="My Co",
        issuer_vat_id="DE123456789", invoice_number="R-1", invoice_date=date(2026, 7, 1),
        line_items=(ExtractedLineItem(
            description="Widget", amount_net=Decimal("100.00"), tax_rate=Decimal("19"),
            amount_tax=Decimal("19.00"), amount_gross=Decimal("119.00"),
        ),),
        tax_breakdown=(TaxBreakdownEntry(Decimal("19"), Decimal("100.00"), Decimal("19.00")),),
        total_net=Decimal("100.00"), total_tax=Decimal("19.00"), total_gross=Decimal("119.00"),
        confidence=Decimal("0.9"),
    )


def _admin() -> Principal:
    role = ROLES["admin"]
    return Principal(
        user_id=OWNER, role_id=role.id, role_label=role.label, clearance=role.clearance,
        locations=None if role.scope == "chain" else frozenset({"berlin"}),
        categories=role.categories, location_label="all", tenant_id=ACCOUNT,
    )


def _env(tmp_path, *, outcomes=("clean",)):
    vectors = MemoryStore()
    drive = MemoryDriveStore(vectors)
    blobs = LocalDriveBlobStore(str(tmp_path / "drive"), min_free_bytes=0, min_free_percent=0)
    jobs = MemoryJobStore()
    accounting = MemoryAccountingStore()
    platform = MemoryPlatformStore()
    platform.create_account(Account(
        id=ACCOUNT, kind="organization", name="Acme", owner_user_id=OWNER,
    ))
    platform.create_space(Space(id=SPACE, account_id=ACCOUNT, kind="business", name="Business"))
    platform.install_app(AppInstallation(
        id="appi_buchhaltung", account_id=ACCOUNT, app_id="buchhaltung",
        enabled_space_ids=(SPACE,),
        allowed_purposes=(
            "accounting_read", "accounting_ingest", "accounting_configure", "accounting_export",
        ),
    ))
    settings = SimpleNamespace(
        drive_max_file_bytes=1024,
        drive_upload_session_seconds=3600,
        drive_policy_mode="storage_and_indexing",
        drive_private_spaces_enabled=False,
        drive_malware_quarantine_bytes=1024 * 1024,
        drive_malware_retry_attempts=5,
        drive_malware_retry_cooldown_seconds=900,
        drive_malware_retry_max_cooldown_seconds=21_600,
        job_max_attempts=3,
    )
    service = DriveService(
        store=drive, blobs=blobs, platform_store=platform, job_store=jobs, settings=settings,
    )
    scanning = DriveMalwareScanningService(
        store=drive, blobs=blobs, scanner=FakeMalwareScanner(outcomes), job_store=jobs,
        platform_store=platform, settings=settings, worker_id="worker_test",
    )
    accounting_service = AccountingService(
        store=accounting, extractor=FakeInvoiceExtractor(_sample_invoice()),
        drive_store=drive, blob_store=blobs, platform_store=platform, settings=settings,
    )
    return SimpleNamespace(
        vectors=vectors, drive=drive, blobs=blobs, jobs=jobs, accounting=accounting,
        platform=platform, settings=settings, service=service, scanning=scanning,
        accounting_service=accounting_service, principal=_admin(),
    )


def _upload(env, *, name, category, index_for_ai=False, payload=b"OneBrain invoice bytes"):
    upload = env.service.create_upload(
        env.principal, account_id=ACCOUNT, space_id=SPACE, folder_id="",
        name=name, size_bytes=len(payload), index_for_ai=index_for_ai,
        idempotency_key=f"up-{name}",
    )
    started, writer = env.service.begin_upload(env.principal, upload.id)
    writer.write(payload)
    uploaded = env.service.finish_upload_content(env.principal, started, writer.finish(), "text/plain")
    _completed, file = env.service.complete_upload(env.principal, uploaded.id)
    if category != file.category:
        # Simulate an accounting-categorised upload without seeding the confidential
        # AccessGroup + membership the upload ACL would otherwise require — these
        # tests exercise the extraction reconcile, not the category ACL.
        env.drive._files[file.id] = replace(env.drive._files[file.id], category=category)
        file = env.drive.get_file(file.id, account_id=ACCOUNT, space_id=SPACE)
    return file


def _claim_scan(env):
    claimed = env.jobs.claim("worker_test", limit=1, lease_seconds=60)
    assert len(claimed) == 1 and claimed[0].type == JOB_DRIVE_REVISION_MALWARE_SCAN
    return claimed[0]


def _crash(_completion):
    raise RuntimeError("worker crashed after the clean verdict, before accounting enqueue")


def _strand(env, monkeypatch, *, name="invoice.pdf"):
    """Return a malware-clean accounting file whose extraction enqueue was lost.

    Reproduces the exact crash window: the verdict commits, then the accounting
    enqueue raises, and the job is left for retry (never marked done). The replay
    path is the terminal early-return, which the caller can assert does NOT heal.
    """
    file = _upload(env, name=name, category=ACCOUNTING_CATEGORY, index_for_ai=False)
    scan_job = _claim_scan(env)
    monkeypatch.setattr(env.scanning, "_enqueue_accounting_extraction_if_needed", _crash)
    with pytest.raises(RuntimeError):
        env.scanning.handle(scan_job)
    return env.drive.get_file(file.id, account_id=ACCOUNT, space_id=SPACE), scan_job


def _make_document(env, file, *, doc_id):
    """Persist a minimal accounting document for a file's current revision."""
    env.accounting.create_document({
        "id": doc_id, "tenant_id": file.tenant_id, "account_id": ACCOUNT, "space_id": SPACE,
        "drive_file_id": file.id, "drive_revision_id": file.current_revision_id,
        "dedup_key": doc_id, "status": "pending", "check_flags": {},
        "issuer_name": "", "invoice_number": "", "direction": "incoming",
    }, [])


def _extract_jobs(env):
    return [job for job in env.jobs._jobs.values() if job.type == JOB_ACCOUNTING_EXTRACT]


def _reconcile(env, *, limit=100):
    return reconcile_pending_extractions(
        drive_store=env.drive, accounting_store=env.accounting, job_store=env.jobs,
        settings=env.settings, account_id=ACCOUNT, space_id=SPACE, limit=limit,
    )


def test_crash_before_enqueue_strands_the_invoice_and_replay_does_not_heal(tmp_path, monkeypatch):
    env = _env(tmp_path)
    file, scan_job = _strand(env, monkeypatch)

    # The verdict is durable...
    scan = env.drive.get_authoritative_malware_scan(
        file.current_revision_id, account_id=ACCOUNT, space_id=SPACE,
    )
    assert scan.status == "clean"
    # ...but the extraction enqueue was lost, and the terminal replay returns early
    # without re-enqueuing — proving the gap this reconcile exists to close.
    assert _extract_jobs(env) == []
    replay = env.scanning.handle(scan_job)
    assert replay["status"] == "clean" and replay["idempotent_replay"] is True
    assert _extract_jobs(env) == []


def test_reconcile_recovers_the_stranded_extraction_idempotently(tmp_path, monkeypatch):
    env = _env(tmp_path)
    file, _scan_job = _strand(env, monkeypatch)

    assert _reconcile(env) == 1
    jobs = _extract_jobs(env)
    assert len(jobs) == 1
    assert jobs[0].payload == {
        "file_id": file.id,
        "revision_id": file.current_revision_id,
        "generation": file.generation,
    }
    # The reconcile enqueued under the canonical shared key (so a later trigger or
    # reconcile lands on the same job, never a duplicate).
    expected_key = accounting_extract_idempotency_key(
        file.id, file.current_revision_id, file.generation,
    )
    assert any(scope[4] == expected_key for scope in env.jobs._idempotency)

    # Until the extraction actually runs the file still lacks a document, so a
    # second pass re-derives the same identity — but the shared key dedupes it, so
    # no second job is ever created.
    assert _reconcile(env) == 1
    assert len(_extract_jobs(env)) == 1


def test_reconcile_stops_once_a_document_exists(tmp_path, monkeypatch):
    env = _env(tmp_path)
    file, _scan_job = _strand(env, monkeypatch)
    assert _reconcile(env) == 1

    # Run the recovered job for real: it extracts and persists the document.
    extract_job = env.jobs.claim("acct_worker", limit=1, lease_seconds=60)[0]
    assert extract_job.type == JOB_ACCOUNTING_EXTRACT
    result = env.accounting_service.handle_extraction_job(extract_job)
    assert result["status"] == "extracted"
    assert env.accounting.document_for_revision(
        ACCOUNT, SPACE, file.id, file.current_revision_id,
    )

    # The document is now the durable idempotency guard — survives even if the job
    # row is later garbage-collected, unlike the enqueue key alone.
    assert _reconcile(env) == 0


def test_reconcile_finds_an_older_stranded_file_behind_documented_newer_ones(tmp_path, monkeypatch):
    env = _env(tmp_path)
    # An OLD invoice stranded by the crash (clean, no document)...
    old_file, _ = _strand(env, monkeypatch, name="old-invoice.pdf")
    # ...and a NEWER invoice that extracted fine (clean + already has a document).
    new_file, _ = _strand(env, monkeypatch, name="new-invoice.pdf")
    _make_document(env, new_file, doc_id="acctdoc_new")

    # Force a deterministic age gap so the stranded file sorts oldest.
    env.drive._files[old_file.id] = replace(
        env.drive._files[old_file.id], updated_at="2026-07-01T00:00:00+00:00",
    )
    env.drive._files[new_file.id] = replace(
        env.drive._files[new_file.id], updated_at="2026-07-20T00:00:00+00:00",
    )

    # With limit=1 a naive newest-first scan would return only the DOCUMENTED newer
    # file and skip it, leaving the older stranded file invisible forever. Excluding
    # documented revisions BEFORE the limit is what still surfaces the older one.
    assert _reconcile(env, limit=1) == 1
    jobs = _extract_jobs(env)
    assert len(jobs) == 1
    assert jobs[0].payload["file_id"] == old_file.id


def test_reconcile_skips_non_accounting_and_quarantined_files(tmp_path):
    env = _env(tmp_path, outcomes=("clean", "infected"))

    # A clean file outside the accounting category is never an extraction candidate.
    general = _upload(env, name="notes.txt", category="general", index_for_ai=False)
    env.scanning.handle(_claim_scan(env))
    assert env.drive.get_authoritative_malware_scan(
        general.current_revision_id, account_id=ACCOUNT, space_id=SPACE,
    ).status == "clean"

    # An accounting file that scanned infected has no clean attestation to act on.
    infected = _upload(env, name="malware.pdf", category=ACCOUNTING_CATEGORY, index_for_ai=False)
    env.scanning.handle(_claim_scan(env))
    assert env.drive.get_authoritative_malware_scan(
        infected.current_revision_id, account_id=ACCOUNT, space_id=SPACE,
    ).status == "infected"

    assert _reconcile(env) == 0
    assert _extract_jobs(env) == []


def test_clean_scan_still_enqueues_extraction_through_the_shared_helper(tmp_path):
    env = _env(tmp_path)
    file = _upload(env, name="invoice.pdf", category=ACCOUNTING_CATEGORY, index_for_ai=False)

    result = env.scanning.handle(_claim_scan(env))

    assert result["status"] == "clean"
    jobs = _extract_jobs(env)
    assert len(jobs) == 1
    expected_key = accounting_extract_idempotency_key(
        file.id, file.current_revision_id, file.generation,
    )
    assert any(scope[4] == expected_key for scope in env.jobs._idempotency)
    # The trigger and the reconcile compute the SAME identity, so a following
    # reconcile lands on the existing job instead of creating a duplicate.
    assert _reconcile(env) == 1
    assert len(_extract_jobs(env)) == 1


def test_reading_the_documents_endpoint_recovers_a_stranded_extraction(tmp_path, monkeypatch):
    env = _env(tmp_path)
    _strand(env, monkeypatch)
    assert _extract_jobs(env) == []

    monkeypatch.setattr(accounting_router, "get_platform_store", lambda: env.platform)
    monkeypatch.setattr(accounting_router, "get_accounting_store", lambda: env.accounting)
    monkeypatch.setattr(accounting_router, "get_drive_store", lambda: env.drive)
    monkeypatch.setattr(accounting_router, "get_job_store", lambda: env.jobs)
    monkeypatch.setattr(accounting_router, "get_settings", lambda: env.settings)

    documents = accounting_router.list_accounting_documents(
        account_id=ACCOUNT, space_id=SPACE, status=None, principal=env.principal,
    )

    # The extraction has not run yet, so the list is still empty — but opening the
    # workspace re-enqueued the stranded invoice.
    assert documents == []
    assert len(_extract_jobs(env)) == 1
