"""Accounting (Buchhaltung) module gate + overview authorization contracts.

Phase 0: the module is off by default. Its endpoints must 403 unless the
``buchhaltung`` app is installed for the space with the ``accounting_read``
purpose, and where it is installed the (empty) overview reads as zeros.
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

import app.routers.accounting as accounting_router
from app.accounting.base import accounting_category_id
from app.accounting.memory import MemoryAccountingStore
from app.accounting.model import ExtractedInvoice, ExtractedLineItem, TaxBreakdownEntry
from app.accounting.service import build_document_row, build_line_item_rows
from app.accounting.validation import needs_review, validate
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.platform.base import (
    AccessGroup,
    AccessGroupMembership,
    Account,
    AppInstallation,
    Membership,
    Space,
)
from app.platform.memory import MemoryPlatformStore


def _seed(accounting, *, doc_id="acctdoc_1", issuer="ACME GmbH", number="R-1",
          direction="incoming", net="1000.00", tax="190.00", gross="1190.00",
          rate="19", file_id="f1", rev="r1"):
    invoice = ExtractedInvoice(
        direction=direction, issuer_name=issuer, recipient_name="My Co",
        issuer_vat_id="DE123456789", invoice_number=number, invoice_date=date(2026, 7, 1),
        line_items=(ExtractedLineItem(
            description="X", amount_net=Decimal(net), tax_rate=Decimal(rate),
            amount_tax=Decimal(tax), amount_gross=Decimal(gross),
        ),),
        tax_breakdown=(TaxBreakdownEntry(Decimal(rate), Decimal(net), Decimal(tax)),),
        total_net=Decimal(net), total_tax=Decimal(tax), total_gross=Decimal(gross),
        confidence=Decimal("0.9"),
    )
    flags, dedup = validate(invoice, file_sha256=file_id)
    flags["duplicate"] = False
    flags["duplicate_of"] = ""
    flags["invoice_number_unique"] = True
    flags["needs_review"] = needs_review(flags)
    now = "2026-07-01T00:00:00+00:00"
    document = build_document_row(
        doc_id, "acme", "sp_business", invoice=invoice, flags=flags, dedup_key=dedup,
        drive_file_id=file_id, drive_revision_id=rev, created_by="admin@acme", now=now,
    )
    lines = build_line_item_rows(doc_id, "acme", "sp_business", invoice=invoice, now=now)
    return accounting.create_document(document, lines)


def _human(role_id: str = "admin", user_id: str = "admin@acme", tenant_id: str = "acme") -> Principal:
    role = ROLES[role_id]
    return Principal(
        user_id=user_id,
        role_id=role.id,
        role_label=role.label,
        clearance=role.clearance,
        locations=None if role.scope == "chain" else frozenset({"berlin"}),
        categories=role.categories,
        location_label="all",
        tenant_id=tenant_id,
    )


def _stores(*, install: bool = True):
    platform = MemoryPlatformStore()
    platform.create_account(Account(
        id="acme", kind="organization", name="Acme", owner_user_id="admin@acme",
    ))
    platform.create_space(Space(
        id="sp_business", account_id="acme", kind="business", name="Business",
    ))
    platform.create_space(Space(
        id="sp_shared", account_id="acme", kind="shared", name="Shared",
    ))
    if install:
        platform.install_app(AppInstallation(
            id="appi_buchhaltung",
            account_id="acme",
            app_id="buchhaltung",
            enabled_space_ids=("sp_business",),
            allowed_purposes=(
                "accounting_read", "accounting_ingest",
                "accounting_configure", "accounting_export",
            ),
        ))
    return platform, MemoryAccountingStore()


def _wire(monkeypatch, *, install: bool = True):
    platform, accounting = _stores(install=install)
    monkeypatch.setattr(accounting_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(accounting_router, "get_accounting_store", lambda: accounting)
    return platform, accounting


def test_installed_workspace_lists_and_reads_empty_overview(monkeypatch):
    _wire(monkeypatch, install=True)
    workspaces = accounting_router.list_accounting_workspaces(principal=_human())
    assert [row.space_id for row in workspaces] == ["sp_business"]
    overview = accounting_router.get_accounting_overview(
        account_id="acme", space_id="sp_business", principal=_human(),
    )
    assert (
        overview.total_documents,
        overview.pending_documents,
        overview.confirmed_documents,
    ) == (0, 0, 0)


def test_gate_is_off_without_installation(monkeypatch):
    _wire(monkeypatch, install=False)
    # No install anywhere → no workspaces, and the overview is forbidden.
    assert accounting_router.list_accounting_workspaces(principal=_human()) == []
    with pytest.raises(HTTPException) as forbidden:
        accounting_router.get_accounting_overview(
            account_id="acme", space_id="sp_business", principal=_human(),
        )
    assert forbidden.value.status_code == 403


def test_overview_forbidden_on_a_space_without_the_app(monkeypatch):
    _wire(monkeypatch, install=True)
    # Installed on sp_business only → sp_shared must stay gated.
    with pytest.raises(HTTPException) as forbidden:
        accounting_router.get_accounting_overview(
            account_id="acme", space_id="sp_shared", principal=_human(),
        )
    assert forbidden.value.status_code in {403, 404}


def test_cross_tenant_ids_fail_closed(monkeypatch):
    _wire(monkeypatch, install=True)
    with pytest.raises(HTTPException) as cross:
        accounting_router.get_accounting_overview(
            account_id="acme",
            space_id="sp_business",
            principal=_human(user_id="admin@other", tenant_id="other"),
        )
    assert cross.value.status_code == 404


# ---- Phase 1: documents, confirm, real overview -----------------------------

def test_lists_documents_and_reads_detail(monkeypatch):
    _, accounting = _wire(monkeypatch, install=True)
    _seed(accounting, doc_id="acctdoc_1", number="R-1")
    _seed(accounting, doc_id="acctdoc_2", number="R-2", file_id="f2", rev="r2")
    documents = accounting_router.list_accounting_documents(
        account_id="acme", space_id="sp_business", status="pending", principal=_human(),
    )
    assert {doc.id for doc in documents} == {"acctdoc_1", "acctdoc_2"}
    detail = accounting_router.get_accounting_document(
        document_id="acctdoc_1", account_id="acme", space_id="sp_business", principal=_human(),
    )
    assert detail.status == "pending"
    assert detail.line_items[0].proposed_account == "4980"
    assert detail.line_items[0].proposed_tax_key == "9"


def test_confirm_single_accepts_proposals_and_counts_in_overview(monkeypatch):
    _, accounting = _wire(monkeypatch, install=True)
    _seed(accounting, doc_id="acctdoc_1")
    body = accounting_router.AccountingConfirmIn(
        account_id="acme", space_id="sp_business",
        confirmations=[accounting_router.AccountingConfirmItemIn(document_id="acctdoc_1")],
    )
    updated = accounting_router.confirm_accounting_documents(body=body, principal=_human())
    assert updated[0].status == "confirmed"
    # Accepting the proposal copies proposed → confirmed.
    assert updated[0].line_items[0].confirmed_account == "4980"

    overview = accounting_router.get_accounting_overview(
        account_id="acme", space_id="sp_business", principal=_human(),
    )
    assert overview.confirmed_documents == 1
    assert overview.pending_documents == 0
    assert overview.incoming.tax == "190.00"
    assert overview.input_vat == "190.00"
    assert overview.vat_balance == "-190.00"


def test_confirm_applies_line_corrections(monkeypatch):
    _, accounting = _wire(monkeypatch, install=True)
    document = _seed(accounting, doc_id="acctdoc_1")
    line_id = document["line_items"][0]["id"]
    body = accounting_router.AccountingConfirmIn(
        account_id="acme", space_id="sp_business",
        confirmations=[accounting_router.AccountingConfirmItemIn(
            document_id="acctdoc_1",
            line_items=[accounting_router.AccountingLineCorrectionIn(
                id=line_id, account="4900", tax_key="8",
            )],
        )],
    )
    updated = accounting_router.confirm_accounting_documents(body=body, principal=_human())
    assert updated[0].line_items[0].confirmed_account == "4900"
    assert updated[0].line_items[0].confirmed_tax_key == "8"


def test_confirm_unknown_document_is_404(monkeypatch):
    _wire(monkeypatch, install=True)
    body = accounting_router.AccountingConfirmIn(
        account_id="acme", space_id="sp_business",
        confirmations=[accounting_router.AccountingConfirmItemIn(document_id="ghost")],
    )
    with pytest.raises(HTTPException) as missing:
        accounting_router.confirm_accounting_documents(body=body, principal=_human())
    assert missing.value.status_code == 404


def test_confirm_requires_the_configure_purpose(monkeypatch):
    platform = MemoryPlatformStore()
    platform.create_account(Account(id="acme", kind="organization", name="Acme", owner_user_id="admin@acme"))
    platform.create_space(Space(id="sp_business", account_id="acme", kind="business", name="Business"))
    platform.install_app(AppInstallation(
        id="appi_buchhaltung", account_id="acme", app_id="buchhaltung",
        enabled_space_ids=("sp_business",), allowed_purposes=("accounting_read",),  # no configure
    ))
    accounting = MemoryAccountingStore()
    monkeypatch.setattr(accounting_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(accounting_router, "get_accounting_store", lambda: accounting)
    _seed(accounting, doc_id="acctdoc_1")
    body = accounting_router.AccountingConfirmIn(
        account_id="acme", space_id="sp_business",
        confirmations=[accounting_router.AccountingConfirmItemIn(document_id="acctdoc_1")],
    )
    with pytest.raises(HTTPException) as forbidden:
        accounting_router.confirm_accounting_documents(body=body, principal=_human())
    assert forbidden.value.status_code == 403


def test_document_detail_unknown_is_404(monkeypatch):
    _wire(monkeypatch, install=True)
    with pytest.raises(HTTPException) as missing:
        accounting_router.get_accounting_document(
            document_id="ghost", account_id="acme", space_id="sp_business", principal=_human(),
        )
    assert missing.value.status_code == 404


def test_reader_must_be_a_category_member_or_admin(monkeypatch):
    platform, accounting = _stores(install=True)
    platform.upsert_membership(Membership(
        id="mem_clerk", account_id="acme", user_id="clerk@acme",
        role_id="finance", space_id="sp_business",
    ))
    monkeypatch.setattr(accounting_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(accounting_router, "get_accounting_store", lambda: accounting)
    clerk = _human(role_id="finance", user_id="clerk@acme", tenant_id="acme")

    # A workspace member who is not in the confidential buchhaltung category is refused.
    with pytest.raises(HTTPException) as blocked:
        accounting_router.get_accounting_overview(
            account_id="acme", space_id="sp_business", principal=clerk,
        )
    assert blocked.value.status_code == 403

    group_id = accounting_category_id("sp_business")
    platform.upsert_access_group(AccessGroup(
        id=group_id, account_id="acme", name="Buchhaltung", kind="department", space_id="sp_business",
    ))
    platform.upsert_access_group_membership(AccessGroupMembership(
        id="agm_clerk", account_id="acme", group_id=group_id,
        user_id="clerk@acme", space_id="sp_business",
    ))
    overview = accounting_router.get_accounting_overview(
        account_id="acme", space_id="sp_business", principal=clerk,
    )
    assert overview.total_documents == 0


def test_confirm_can_clear_the_tax_key(monkeypatch):
    _, accounting = _wire(monkeypatch, install=True)
    document = _seed(accounting, doc_id="acctdoc_1")
    line_id = document["line_items"][0]["id"]
    body = accounting_router.AccountingConfirmIn(
        account_id="acme", space_id="sp_business",
        confirmations=[accounting_router.AccountingConfirmItemIn(
            document_id="acctdoc_1",
            line_items=[accounting_router.AccountingLineCorrectionIn(
                id=line_id, account="4980", tax_key="",  # explicit empty = tax-exempt
            )],
        )],
    )
    updated = accounting_router.confirm_accounting_documents(body=body, principal=_human())
    assert updated[0].line_items[0].confirmed_tax_key == ""  # honoured, not fallen back to "9"


def test_confirm_reproposes_lines_when_direction_flips(monkeypatch):
    _, accounting = _wire(monkeypatch, install=True)
    _seed(accounting, doc_id="acctdoc_1", direction="incoming")  # proposed 4980 / key 9
    body = accounting_router.AccountingConfirmIn(
        account_id="acme", space_id="sp_business",
        confirmations=[accounting_router.AccountingConfirmItemIn(
            document_id="acctdoc_1", direction="outgoing",  # flip, no explicit line override
        )],
    )
    updated = accounting_router.confirm_accounting_documents(body=body, principal=_human())
    assert updated[0].direction == "outgoing"
    # Re-proposed for the new direction instead of keeping the incoming booking.
    assert updated[0].line_items[0].confirmed_account == "8400"
    assert updated[0].line_items[0].confirmed_tax_key == "3"
