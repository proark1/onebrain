"""Human-facing KPI dashboard and administration endpoints."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth.account_access import is_account_admin, is_account_member
from app.auth.principal import Principal, resolve_principal
from app.deps import get_kpi_store, get_platform_store
from app.kpis.access import (
    authorize_kpi_configurer,
    authorize_kpi_manual_writer,
    authorize_kpi_reader,
)
from app.kpis.base import (
    KPI_APP_ID,
    KPI_CONFIGURE_PURPOSE,
    KPI_READ_PURPOSE,
    KPI_SNAPSHOT_WRITE_PURPOSE,
    KpiConflictError,
    KpiDefinition,
    KpiLimitError,
    KpiSeries,
    KpiSnapshot,
    freshness_state,
    normalize_decimal,
    normalize_optional_decimal,
    normalize_timestamp,
    now_iso,
    threshold_state,
)
from app.platform.base import AuditEvent


router = APIRouter(prefix="/api/kpis", tags=["kpis"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KpiWorkspaceOut(StrictModel):
    account_id: str
    account_name: str
    space_id: str
    space_name: str
    space_kind: str
    can_configure: bool
    can_write_manual: bool


class KpiDefinitionCreate(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    key: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    category: str = Field(default="", max_length=80)
    unit: str = Field(default="", max_length=32)
    source_label: str = Field(default="", max_length=120)
    owner_label: str = Field(default="", max_length=120)
    freshness_minutes: int = Field(default=1440, ge=1, le=525_600)
    warning_min: Optional[Decimal] = None
    warning_max: Optional[Decimal] = None
    critical_min: Optional[Decimal] = None
    critical_max: Optional[Decimal] = None
    display_order: int = Field(default=0, ge=-1_000_000, le=1_000_000)


class KpiDefinitionUpdate(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    key: Optional[str] = Field(default=None, min_length=2, max_length=64)
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    category: Optional[str] = Field(default=None, max_length=80)
    unit: Optional[str] = Field(default=None, max_length=32)
    source_label: Optional[str] = Field(default=None, max_length=120)
    owner_label: Optional[str] = Field(default=None, max_length=120)
    freshness_minutes: Optional[int] = Field(default=None, ge=1, le=525_600)
    warning_min: Optional[Decimal] = None
    warning_max: Optional[Decimal] = None
    critical_min: Optional[Decimal] = None
    critical_max: Optional[Decimal] = None
    display_order: Optional[int] = Field(default=None, ge=-1_000_000, le=1_000_000)
    status: Optional[Literal["active", "archived"]] = None


class KpiSnapshotCreate(StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    value: Decimal
    observed_at: str = Field(min_length=1, max_length=80)
    source_ref: str = Field(default="manual", max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=128)


class KpiSnapshotOut(StrictModel):
    id: str
    kpi_id: str
    value: str
    observed_at: str
    received_at: str
    source_ref: str


class KpiDefinitionOut(StrictModel):
    id: str
    account_id: str
    space_id: str
    key: str
    name: str
    description: str
    category: str
    unit: str
    source_label: str
    owner_label: str
    freshness_minutes: int
    warning_min: Optional[str]
    warning_max: Optional[str]
    critical_min: Optional[str]
    critical_max: Optional[str]
    display_order: int
    status: str
    created_at: str
    updated_at: str


class KpiDashboardItemOut(StrictModel):
    definition: KpiDefinitionOut
    latest: Optional[KpiSnapshotOut]
    previous: Optional[KpiSnapshotOut]
    history: list[KpiSnapshotOut]
    absolute_delta: Optional[str]
    percentage_delta: Optional[str]
    threshold_state: Literal["healthy", "warning", "critical", "awaiting_data"]
    freshness_state: Literal["fresh", "stale", "awaiting_data"]


class KpiDashboardOut(StrictModel):
    account_id: str
    space_id: str
    generated_at: str
    can_configure: bool
    can_write_manual: bool
    items: list[KpiDashboardItemOut]


class KpiIngestOut(StrictModel):
    accepted_count: int
    duplicate_count: int
    snapshots: list[KpiSnapshotOut]


@router.get("/workspaces", response_model=list[KpiWorkspaceOut])
def list_kpi_workspaces(principal: Principal = Depends(resolve_principal)):
    if principal.principal_type != "human":
        raise HTTPException(status_code=403, detail="Human session required.")
    platform = get_platform_store()
    account = platform.get_account(principal.tenant_id)
    if not account:
        return []
    admin = is_account_admin(principal, account, platform)
    workspaces: list[KpiWorkspaceOut] = []
    for space in platform.list_spaces(account.id):
        if not is_account_member(principal, account, space.id, platform):
            continue
        read = platform.check_app_access(account.id, KPI_APP_ID, space.id, KPI_READ_PURPOSE)
        if not read.allowed:
            continue
        configure = platform.check_app_access(
            account.id, KPI_APP_ID, space.id, KPI_CONFIGURE_PURPOSE,
        ).allowed
        write = platform.check_app_access(
            account.id, KPI_APP_ID, space.id, KPI_SNAPSHOT_WRITE_PURPOSE,
        ).allowed
        workspaces.append(KpiWorkspaceOut(
            account_id=account.id,
            account_name=account.name,
            space_id=space.id,
            space_name=space.name,
            space_kind=space.kind,
            can_configure=admin and configure,
            can_write_manual=admin and write,
        ))
    return workspaces


@router.get("", response_model=KpiDashboardOut)
def get_kpi_dashboard(
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    history_limit: Annotated[int, Query(ge=1, le=366)] = 30,
    include_archived: bool = False,
    principal: Principal = Depends(resolve_principal),
):
    platform = get_platform_store()
    account, _ = authorize_kpi_reader(principal, account_id, space_id, platform)
    admin = is_account_admin(principal, account, platform)
    if include_archived and not admin:
        raise HTTPException(status_code=403, detail="Admin role required to include archived KPIs.")
    can_configure = admin and platform.check_app_access(
        account_id, KPI_APP_ID, space_id, KPI_CONFIGURE_PURPOSE,
    ).allowed
    can_write_manual = admin and platform.check_app_access(
        account_id, KPI_APP_ID, space_id, KPI_SNAPSHOT_WRITE_PURPOSE,
    ).allowed
    generated = datetime.now(timezone.utc)
    series = get_kpi_store().dashboard(
        account_id,
        space_id,
        history_limit=history_limit,
        include_archived=include_archived,
    )
    return KpiDashboardOut(
        account_id=account_id,
        space_id=space_id,
        generated_at=generated.isoformat(),
        can_configure=can_configure,
        can_write_manual=can_write_manual,
        items=[_series_out(row, generated) for row in series],
    )


@router.post("", response_model=KpiDefinitionOut)
def create_kpi_definition(
    body: KpiDefinitionCreate,
    principal: Principal = Depends(resolve_principal),
):
    platform = get_platform_store()
    authorize_kpi_configurer(principal, body.account_id, body.space_id, platform)
    definition = KpiDefinition(
        id=f"kpi_{uuid4().hex}",
        account_id=body.account_id.strip(),
        space_id=body.space_id.strip(),
        key=body.key.strip(),
        name=body.name.strip(),
        description=body.description.strip(),
        category=body.category.strip(),
        unit=body.unit.strip(),
        source_label=body.source_label.strip(),
        owner_label=body.owner_label.strip(),
        freshness_minutes=body.freshness_minutes,
        warning_min=normalize_optional_decimal(body.warning_min),
        warning_max=normalize_optional_decimal(body.warning_max),
        critical_min=normalize_optional_decimal(body.critical_min),
        critical_max=normalize_optional_decimal(body.critical_max),
        display_order=body.display_order,
    )
    try:
        saved = get_kpi_store().create_definition(definition)
    except Exception as exc:
        _raise_store_error(exc)
    _record_audit(
        principal,
        action="kpi_definition.created",
        account_id=saved.account_id,
        space_id=saved.space_id,
        target_id=saved.id,
        meta={"key": saved.key},
    )
    return _definition_out(saved)


@router.patch("/{kpi_id}", response_model=KpiDefinitionOut)
def update_kpi_definition(
    kpi_id: str,
    body: KpiDefinitionUpdate,
    principal: Principal = Depends(resolve_principal),
):
    platform = get_platform_store()
    authorize_kpi_configurer(principal, body.account_id, body.space_id, platform)
    store = get_kpi_store()
    current = store.get_definition(
        kpi_id, account_id=body.account_id, space_id=body.space_id,
    )
    if not current:
        raise HTTPException(status_code=404, detail="KPI definition not found.")

    def value(field: str, current_value):
        return getattr(body, field) if field in body.model_fields_set else current_value

    updated = replace(
        current,
        key=(value("key", current.key) or "").strip(),
        name=(value("name", current.name) or "").strip(),
        description=(value("description", current.description) or "").strip(),
        category=(value("category", current.category) or "").strip(),
        unit=(value("unit", current.unit) or "").strip(),
        source_label=(value("source_label", current.source_label) or "").strip(),
        owner_label=(value("owner_label", current.owner_label) or "").strip(),
        freshness_minutes=value("freshness_minutes", current.freshness_minutes),
        warning_min=normalize_optional_decimal(value("warning_min", current.warning_min)),
        warning_max=normalize_optional_decimal(value("warning_max", current.warning_max)),
        critical_min=normalize_optional_decimal(value("critical_min", current.critical_min)),
        critical_max=normalize_optional_decimal(value("critical_max", current.critical_max)),
        display_order=value("display_order", current.display_order),
        status=value("status", current.status),
    )
    try:
        saved = store.update_definition(updated)
    except Exception as exc:
        _raise_store_error(exc)
    _record_audit(
        principal,
        action="kpi_definition.updated",
        account_id=saved.account_id,
        space_id=saved.space_id,
        target_id=saved.id,
        meta={"key": saved.key, "status": saved.status},
    )
    return _definition_out(saved)


@router.get("/{kpi_id}/snapshots", response_model=list[KpiSnapshotOut])
def list_kpi_snapshots(
    kpi_id: str,
    account_id: Annotated[str, Query(min_length=1, max_length=120)],
    space_id: Annotated[str, Query(min_length=1, max_length=120)],
    limit: Annotated[int, Query(ge=1, le=366)] = 30,
    principal: Principal = Depends(resolve_principal),
):
    authorize_kpi_reader(principal, account_id, space_id, get_platform_store())
    store = get_kpi_store()
    if not store.get_definition(kpi_id, account_id=account_id, space_id=space_id):
        raise HTTPException(status_code=404, detail="KPI definition not found.")
    return [
        _snapshot_out(row)
        for row in store.list_snapshots(
            kpi_id, account_id=account_id, space_id=space_id, limit=limit,
        )
    ]


@router.post("/{kpi_id}/snapshots", response_model=KpiIngestOut)
def create_manual_kpi_snapshot(
    kpi_id: str,
    body: KpiSnapshotCreate,
    principal: Principal = Depends(resolve_principal),
):
    authorize_kpi_manual_writer(
        principal, body.account_id, body.space_id, get_platform_store(),
    )
    store = get_kpi_store()
    definition = store.get_definition(
        kpi_id, account_id=body.account_id, space_id=body.space_id,
    )
    if not definition:
        raise HTTPException(status_code=404, detail="KPI definition not found.")
    snapshot = KpiSnapshot(
        id=f"kpisnap_{uuid4().hex}",
        account_id=body.account_id,
        space_id=body.space_id,
        kpi_id=definition.id,
        value=normalize_decimal(body.value),
        observed_at=normalize_timestamp(body.observed_at),
        received_at=now_iso(),
        source_ref=body.source_ref.strip(),
        idempotency_key=body.idempotency_key.strip(),
        created_by=principal.user_id,
    )
    try:
        result = store.ingest_snapshots([snapshot])
    except Exception as exc:
        _raise_store_error(exc)
    _record_audit(
        principal,
        action="kpi_snapshot.manual_recorded",
        account_id=body.account_id,
        space_id=body.space_id,
        target_id=definition.id,
        purpose=KPI_SNAPSHOT_WRITE_PURPOSE,
        meta={
            "snapshot_ids": [row.id for row in result.snapshots],
            "accepted_count": result.accepted_count,
            "duplicate_count": result.duplicate_count,
        },
    )
    return KpiIngestOut(
        accepted_count=result.accepted_count,
        duplicate_count=result.duplicate_count,
        snapshots=[_snapshot_out(row) for row in result.snapshots],
    )


def _definition_out(definition: KpiDefinition) -> KpiDefinitionOut:
    optional = lambda value: str(value) if value is not None else None
    return KpiDefinitionOut(
        id=definition.id,
        account_id=definition.account_id,
        space_id=definition.space_id,
        key=definition.key,
        name=definition.name,
        description=definition.description,
        category=definition.category,
        unit=definition.unit,
        source_label=definition.source_label,
        owner_label=definition.owner_label,
        freshness_minutes=definition.freshness_minutes,
        warning_min=optional(definition.warning_min),
        warning_max=optional(definition.warning_max),
        critical_min=optional(definition.critical_min),
        critical_max=optional(definition.critical_max),
        display_order=definition.display_order,
        status=definition.status,
        created_at=definition.created_at,
        updated_at=definition.updated_at,
    )


def _snapshot_out(snapshot: KpiSnapshot) -> KpiSnapshotOut:
    return KpiSnapshotOut(
        id=snapshot.id,
        kpi_id=snapshot.kpi_id,
        value=str(snapshot.value),
        observed_at=snapshot.observed_at,
        received_at=snapshot.received_at,
        source_ref=snapshot.source_ref,
    )


def _series_out(series: KpiSeries, generated: datetime) -> KpiDashboardItemOut:
    latest = series.snapshots[-1] if series.snapshots else None
    previous = series.snapshots[-2] if len(series.snapshots) > 1 else None
    absolute_delta = latest.value - previous.value if latest and previous else None
    percentage_delta = None
    if absolute_delta is not None and previous and previous.value != 0:
        percentage_delta = absolute_delta / abs(previous.value) * Decimal("100")
    return KpiDashboardItemOut(
        definition=_definition_out(series.definition),
        latest=_snapshot_out(latest) if latest else None,
        previous=_snapshot_out(previous) if previous else None,
        history=[_snapshot_out(row) for row in series.snapshots],
        absolute_delta=str(absolute_delta) if absolute_delta is not None else None,
        percentage_delta=str(percentage_delta) if percentage_delta is not None else None,
        threshold_state=threshold_state(series.definition, latest.value) if latest else "awaiting_data",
        freshness_state=freshness_state(series.definition, latest, now=generated),
    )


def _record_audit(
    principal: Principal,
    *,
    action: str,
    account_id: str,
    space_id: str,
    target_id: str,
    meta: dict,
    purpose: str = KPI_CONFIGURE_PURPOSE,
) -> None:
    get_platform_store().record_audit(AuditEvent(
        id=f"aud_kpi_{uuid4().hex}",
        account_id=account_id,
        actor_id=principal.user_id,
        actor_type=principal.principal_type,
        action=action,
        target_type="kpi_definition",
        target_id=target_id,
        space_id=space_id,
        app_id=KPI_APP_ID,
        purpose=purpose,
        decision="recorded",
        meta=meta,
    ))


def _raise_store_error(exc: Exception):
    if isinstance(exc, (KpiConflictError, KpiLimitError)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail="KPI definition not found.") from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc
