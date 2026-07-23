"""Human-facing accounting (Buchhaltung) module endpoints.

Phase 0 is the module skeleton. It proves the per-workspace install gate (the
KPI Dashboard / AI Employees template: a ``buchhaltung`` ``AppInstallation`` with
the ``accounting_read`` purpose) and returns an empty overview from the
still-empty tables. The capture/extraction pipeline, booking proposals, and the
governed query surface land in later phases.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from app.accounting.access import authorize_accounting_reader
from app.accounting.base import ACCOUNTING_APP_ID, ACCOUNTING_READ_PURPOSE
from app.auth.account_access import is_account_member
from app.auth.principal import Principal, resolve_principal
from app.deps import get_accounting_store, get_platform_store


router = APIRouter(prefix="/api/accounting", tags=["accounting"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountingWorkspaceOut(StrictModel):
    account_id: str
    account_name: str
    space_id: str
    space_name: str
    space_kind: str


class AccountingOverviewOut(StrictModel):
    account_id: str
    space_id: str
    total_documents: int
    pending_documents: int
    confirmed_documents: int


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


@router.get("", response_model=AccountingOverviewOut)
def get_accounting_overview(
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    principal: Principal = Depends(resolve_principal),
):
    """Aggregate document counts for one workspace (403 unless Accounting is enabled)."""
    platform = get_platform_store()
    authorize_accounting_reader(principal, account_id, space_id, platform)
    overview = get_accounting_store().overview(account_id, space_id)
    return AccountingOverviewOut(
        account_id=overview.account_id,
        space_id=overview.space_id,
        total_documents=overview.total_documents,
        pending_documents=overview.pending_documents,
        confirmed_documents=overview.confirmed_documents,
    )
