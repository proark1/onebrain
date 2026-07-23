"""Human-facing accounting (Buchhaltung) module endpoints.

Phase 1 exposes the review-and-confirm surface over the extracted documents:
list the pending drafts (with their validation flags + per-line booking
proposals), read one in detail, and confirm — one document or a batch of clean
ones — which is the only thing that turns ``pending`` into ``confirmed`` and lets
it count in the overview. Every route is behind the per-workspace install gate;
reads need the ``accounting_read`` purpose, confirms the ``accounting_configure``
purpose. Extraction itself is driven by the Drive malware-clean trigger, not here.
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.accounting.access import authorize_accounting_reader, authorize_accounting_writer
from app.accounting.base import (
    ACCOUNTING_APP_ID,
    ACCOUNTING_CONFIGURE_PURPOSE,
    ACCOUNTING_READ_PURPOSE,
    DOCUMENT_STATUSES,
)
from app.accounting.booking import propose_line
from app.accounting.model import to_rate
from app.auth.account_access import is_account_member
from app.auth.principal import Principal, resolve_principal
from app.deps import get_accounting_store, get_platform_store
from app.platform.base import AuditEvent


router = APIRouter(prefix="/api/accounting", tags=["accounting"])

_ID = Query(min_length=1, max_length=120)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountingWorkspaceOut(StrictModel):
    account_id: str
    account_name: str
    space_id: str
    space_name: str
    space_kind: str


class AccountingSideOut(StrictModel):
    count: int
    net: str
    tax: str
    gross: str


class AccountingSummaryOut(StrictModel):
    account_id: str
    space_id: str
    currency: str
    total_documents: int
    pending_documents: int
    confirmed_documents: int
    incoming: AccountingSideOut
    outgoing: AccountingSideOut
    input_vat: str
    output_vat: str
    vat_balance: str


class AccountingLineItemOut(StrictModel):
    id: str
    line_no: int
    description: str
    amount_net: Optional[str] = None
    tax_rate: Optional[str] = None
    amount_tax: Optional[str] = None
    amount_gross: Optional[str] = None
    proposed_account: str
    confirmed_account: str
    proposed_tax_key: str
    confirmed_tax_key: str
    cost_center: str


class AccountingDocumentOut(StrictModel):
    id: str
    direction: str
    issuer_name: str
    recipient_name: str
    invoice_number: str
    invoice_date: Optional[str] = None
    service_date: Optional[str] = None
    currency: str
    total_net: Optional[str] = None
    total_tax: Optional[str] = None
    total_gross: Optional[str] = None
    tax_breakdown: list = Field(default_factory=list)
    dedup_key: str
    check_flags: dict = Field(default_factory=dict)
    status: str
    confidence: Optional[str] = None
    jurisdiction: str
    drive_file_id: str
    drive_revision_id: str
    created_by: str
    confirmed_by: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    line_items: list[AccountingLineItemOut] = Field(default_factory=list)


class AccountingLineCorrectionIn(StrictModel):
    id: str = Field(min_length=1, max_length=120)
    account: Optional[str] = Field(default=None, max_length=32)
    tax_key: Optional[str] = Field(default=None, max_length=16)
    cost_center: Optional[str] = Field(default=None, max_length=64)


class AccountingConfirmItemIn(StrictModel):
    document_id: str = Field(min_length=1, max_length=120)
    direction: Optional[Literal["incoming", "outgoing"]] = None
    line_items: list[AccountingLineCorrectionIn] = Field(default_factory=list, max_length=200)


class AccountingConfirmIn(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    confirmations: list[AccountingConfirmItemIn] = Field(default_factory=list, max_length=500)


def _line_out(row: dict) -> AccountingLineItemOut:
    return AccountingLineItemOut(
        id=row.get("id", ""),
        line_no=int(row.get("line_no") or 0),
        description=row.get("description") or "",
        amount_net=row.get("amount_net"),
        tax_rate=row.get("tax_rate"),
        amount_tax=row.get("amount_tax"),
        amount_gross=row.get("amount_gross"),
        proposed_account=row.get("proposed_account") or "",
        confirmed_account=row.get("confirmed_account") or "",
        proposed_tax_key=row.get("proposed_tax_key") or "",
        confirmed_tax_key=row.get("confirmed_tax_key") or "",
        cost_center=row.get("cost_center") or "",
    )


def _document_out(doc: dict) -> AccountingDocumentOut:
    return AccountingDocumentOut(
        id=doc.get("id", ""),
        direction=doc.get("direction") or "incoming",
        issuer_name=doc.get("issuer_name") or "",
        recipient_name=doc.get("recipient_name") or "",
        invoice_number=doc.get("invoice_number") or "",
        invoice_date=doc.get("invoice_date"),
        service_date=doc.get("service_date"),
        currency=doc.get("currency") or "EUR",
        total_net=doc.get("total_net"),
        total_tax=doc.get("total_tax"),
        total_gross=doc.get("total_gross"),
        tax_breakdown=doc.get("tax_breakdown") or [],
        dedup_key=doc.get("dedup_key") or "",
        check_flags=doc.get("check_flags") or {},
        status=doc.get("status") or "pending",
        confidence=doc.get("confidence"),
        jurisdiction=doc.get("jurisdiction") or "DE",
        drive_file_id=doc.get("drive_file_id") or "",
        drive_revision_id=doc.get("drive_revision_id") or "",
        created_by=doc.get("created_by") or "",
        confirmed_by=doc.get("confirmed_by") or "",
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
        line_items=[_line_out(line) for line in doc.get("line_items", [])],
    )


def _summary_out(summary: dict) -> AccountingSummaryOut:
    return AccountingSummaryOut(
        account_id=summary["account_id"],
        space_id=summary["space_id"],
        currency=summary["currency"],
        total_documents=summary["total_documents"],
        pending_documents=summary["pending_documents"],
        confirmed_documents=summary["confirmed_documents"],
        incoming=AccountingSideOut(**summary["incoming"]),
        outgoing=AccountingSideOut(**summary["outgoing"]),
        input_vat=summary["input_vat"],
        output_vat=summary["output_vat"],
        vat_balance=summary["vat_balance"],
    )


@router.get("/workspaces", response_model=list[AccountingWorkspaceOut])
def list_accounting_workspaces(principal: Principal = Depends(resolve_principal)):
    """List the spaces where Accounting is installed and readable for this caller."""
    if principal.principal_type != "human":
        raise HTTPException(status_code=403, detail="Human session required.")
    platform = get_platform_store()
    account = platform.get_account(principal.tenant_id)
    if not account:
        return []
    workspaces: list[AccountingWorkspaceOut] = []
    for space in platform.list_spaces(account.id):
        if not is_account_member(principal, account, space.id, platform):
            continue
        read = platform.check_app_access(
            account.id, ACCOUNTING_APP_ID, space.id, ACCOUNTING_READ_PURPOSE,
        )
        if not read.allowed:
            continue
        workspaces.append(AccountingWorkspaceOut(
            account_id=account.id,
            account_name=account.name,
            space_id=space.id,
            space_name=space.name,
            space_kind=space.kind,
        ))
    return workspaces


@router.get("", response_model=AccountingSummaryOut)
def get_accounting_overview(
    account_id: Annotated[str, _ID],
    space_id: Annotated[str, _ID],
    principal: Principal = Depends(resolve_principal),
):
    """Workspace dashboard: counts + confirmed-only net/VAT per direction (403 unless enabled)."""
    platform = get_platform_store()
    authorize_accounting_reader(principal, account_id, space_id, platform)
    return _summary_out(get_accounting_store().summary(account_id, space_id))


@router.get("/documents", response_model=list[AccountingDocumentOut])
def list_accounting_documents(
    account_id: Annotated[str, _ID],
    space_id: Annotated[str, _ID],
    status: Annotated[Optional[str], Query(max_length=32)] = None,
    principal: Principal = Depends(resolve_principal),
):
    """List documents (optionally filtered to pending/confirmed) with flags + proposals."""
    if status and status not in DOCUMENT_STATUSES:
        raise HTTPException(status_code=400, detail="Unknown status filter.")
    platform = get_platform_store()
    authorize_accounting_reader(principal, account_id, space_id, platform)
    documents = get_accounting_store().list_documents(account_id, space_id, status or "")
    return [_document_out(document) for document in documents]


@router.post("/documents/confirm", response_model=list[AccountingDocumentOut])
def confirm_accounting_documents(
    body: AccountingConfirmIn,
    principal: Principal = Depends(resolve_principal),
):
    """Confirm one document or a batch of clean ones (accept or correct proposals)."""
    if not body.confirmations:
        raise HTTPException(status_code=400, detail="No documents to confirm.")
    platform = get_platform_store()
    authorize_accounting_writer(principal, body.account_id, body.space_id, platform)
    store = get_accounting_store()
    confirmations = [
        _resolve_confirmation(store, body.account_id, body.space_id, item)
        for item in body.confirmations
    ]
    try:
        updated = store.confirm_documents(
            body.account_id, body.space_id, confirmations, principal.user_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Document not found.")
    _record_confirm_audit(
        platform, principal, body.account_id, body.space_id,
        [confirmation["document_id"] for confirmation in confirmations],
    )
    return [_document_out(document) for document in updated]


@router.get("/documents/{document_id}", response_model=AccountingDocumentOut)
def get_accounting_document(
    document_id: Annotated[str, Path(min_length=1, max_length=120)],
    account_id: Annotated[str, _ID],
    space_id: Annotated[str, _ID],
    principal: Principal = Depends(resolve_principal),
):
    """One document with its line items, booking proposals, and validation flags."""
    platform = get_platform_store()
    authorize_accounting_reader(principal, account_id, space_id, platform)
    document = get_accounting_store().get_document(account_id, space_id, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found.")
    return _document_out(document)


def _resolve_confirmation(store, account_id: str, space_id: str, item) -> dict:
    """Turn a confirm item into a store payload, re-proposing bookings on a direction flip.

    If the reviewer corrects the direction (incoming↔outgoing) without overriding every
    line, the untouched lines would otherwise keep the extractor's opposite-direction
    account/key (e.g. a 4980 Vorsteuer line on an outgoing revenue doc). Re-propose those
    lines for the new direction; explicit line corrections always win.
    """
    payload = item.model_dump(exclude_unset=True)
    if not item.direction:
        return payload
    document = store.get_document(account_id, space_id, item.document_id)
    if not document or document.get("direction") == item.direction:
        return payload
    flags = document.get("check_flags") or {}
    explicit_ids = {correction["id"] for correction in payload.get("line_items", [])}
    reproposed = []
    for line in document.get("line_items", []):
        if line["id"] in explicit_ids:
            continue
        proposal = propose_line(
            item.direction, to_rate(line.get("tax_rate")),
            reverse_charge=bool(flags.get("reverse_charge")),
            intra_community=bool(flags.get("intra_community")),
        )
        reproposed.append({"id": line["id"], "account": proposal.account, "tax_key": proposal.tax_key})
    if reproposed:
        payload["line_items"] = payload.get("line_items", []) + reproposed
    return payload


def _record_confirm_audit(platform, principal, account_id, space_id, document_ids) -> None:
    platform.record_audit(AuditEvent(
        id=f"aud_acct_{uuid4().hex}",
        account_id=account_id,
        actor_id=getattr(principal, "user_id", ""),
        actor_type=principal.principal_type,
        action="accounting.documents_confirmed",
        target_type="accounting_workspace",
        target_id=space_id,
        space_id=space_id,
        app_id=ACCOUNTING_APP_ID,
        purpose=ACCOUNTING_CONFIGURE_PURPOSE,
        decision="recorded",
        meta={"count": len(document_ids), "document_ids": document_ids[:50]},
    ))
