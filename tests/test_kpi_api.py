"""KPI human and machine API authorization and ingestion contracts."""

from dataclasses import replace
from decimal import Decimal

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.routers.kpis as kpi_router
import app.routers.service as service_router
from app.auth.principal import Principal
from app.auth.roles import ROLES
from app.kpis.memory import MemoryKpiStore
from app.platform.base import Account, AppInstallation, Membership, Space
from app.platform.memory import MemoryPlatformStore
from app.security.policy import Classification
from app.servicekeys.base import SCOPE_READ, SCOPE_WRITE


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


def _service(**changes) -> Principal:
    base = Principal(
        user_id="svc:kpi-key",
        role_id="service",
        role_label="Service",
        clearance=Classification.PUBLIC,
        locations=frozenset(),
        categories=frozenset({"general"}),
        location_label="-",
        tenant_id="acme",
        principal_type="service",
        scopes=frozenset({SCOPE_WRITE}),
        account_id="acme",
        space_ids=frozenset({"sp_business"}),
        app_id="kpi_dashboard",
        purposes=frozenset({"kpi_snapshot_write"}),
    )
    return replace(base, **changes)


def _stores():
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
    platform.install_app(AppInstallation(
        id="appi_kpi",
        account_id="acme",
        app_id="kpi_dashboard",
        enabled_space_ids=("sp_business",),
        allowed_purposes=("kpi_read", "kpi_configure", "kpi_snapshot_write"),
    ))
    return platform, MemoryKpiStore()


def _wire(monkeypatch):
    platform, kpis = _stores()
    monkeypatch.setattr(kpi_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(kpi_router, "get_kpi_store", lambda: kpis)
    monkeypatch.setattr(service_router, "get_platform_store", lambda: platform)
    monkeypatch.setattr(service_router, "get_kpi_store", lambda: kpis)
    monkeypatch.setattr(service_router, "_rate_limit", lambda principal: None)
    return platform, kpis


def _definition_body(**changes):
    values = {
        "account_id": "acme",
        "space_id": "sp_business",
        "key": "gross_margin",
        "name": "Gross margin",
        "category": "Finance",
        "unit": "%",
        "freshness_minutes": 60,
        "warning_min": Decimal("60"),
        "critical_min": Decimal("50"),
    }
    values.update(changes)
    return kpi_router.KpiDefinitionCreate(**values)


def test_workspace_discovery_and_live_dashboard_are_authorized(monkeypatch):
    platform, _ = _wire(monkeypatch)
    created = kpi_router.create_kpi_definition(_definition_body(), principal=_human())
    kpi_router.create_manual_kpi_snapshot(
        created.id,
        kpi_router.KpiSnapshotCreate(
            account_id="acme",
            space_id="sp_business",
            value=Decimal("68"),
            observed_at="2026-07-15T09:30:00Z",
            idempotency_key="manual:gross_margin:1",
        ),
        principal=_human(),
    )

    workspaces = kpi_router.list_kpi_workspaces(principal=_human())
    assert [(row.space_id, row.can_configure) for row in workspaces] == [("sp_business", True)]
    dashboard = kpi_router.get_kpi_dashboard(
        account_id="acme",
        space_id="sp_business",
        history_limit=30,
        principal=_human(),
    )
    assert dashboard.items[0].definition.key == "gross_margin"
    assert dashboard.items[0].latest.value == "68"
    assert dashboard.items[0].threshold_state == "healthy"
    audit = platform.list_audit("acme")
    assert {row.action for row in audit} >= {
        "kpi_definition.created", "kpi_snapshot.manual_recorded",
    }
    assert "value" not in audit[-1].meta


def test_member_can_read_only_its_enabled_space(monkeypatch):
    platform, _ = _wire(monkeypatch)
    platform.upsert_membership(Membership(
        id="member_1",
        account_id="acme",
        user_id="finance@acme",
        role_id="viewer",
        space_id="sp_business",
    ))
    kpi_router.create_kpi_definition(_definition_body(), principal=_human())
    viewer = _human(role_id="finance", user_id="finance@acme")

    assert [row.space_id for row in kpi_router.list_kpi_workspaces(principal=viewer)] == ["sp_business"]
    assert kpi_router.get_kpi_dashboard(
        account_id="acme", space_id="sp_business", principal=viewer,
    ).can_configure is False
    with pytest.raises(HTTPException) as forbidden:
        kpi_router.create_kpi_definition(_definition_body(key="arr", name="ARR"), principal=viewer)
    assert forbidden.value.status_code == 403
    with pytest.raises(HTTPException) as unavailable:
        kpi_router.get_kpi_dashboard(
            account_id="acme", space_id="sp_shared", principal=viewer,
        )
    assert unavailable.value.status_code in {403, 404}


def test_cross_tenant_ids_and_unknown_request_fields_fail_closed(monkeypatch):
    _wire(monkeypatch)
    with pytest.raises(HTTPException) as cross:
        kpi_router.get_kpi_dashboard(
            account_id="acme",
            space_id="sp_business",
            principal=_human(user_id="admin@other", tenant_id="other"),
        )
    assert cross.value.status_code == 404

    with pytest.raises(ValidationError):
        kpi_router.KpiDefinitionCreate(**{
            **_definition_body().model_dump(),
            "created_at": "caller-controlled",
        })
    with pytest.raises(ValidationError):
        service_router.ServiceKpiSnapshotBatch(
            space_id="sp_business",
            account_id="other",
            snapshots=[],
        )


def test_service_batch_ingestion_is_scoped_idempotent_and_value_free_in_audit(monkeypatch):
    platform, _ = _wire(monkeypatch)
    definition = kpi_router.create_kpi_definition(_definition_body(), principal=_human())
    body = service_router.ServiceKpiSnapshotBatch(
        space_id="sp_business",
        snapshots=[service_router.ServiceKpiSnapshotItem(
            kpi_key="gross_margin",
            value=Decimal("68.25"),
            observed_at="2026-07-15T09:30:00Z",
            source_ref="erp-summary-1",
            idempotency_key="erp:gross_margin:2026-07-15T09:30Z",
        )],
    )

    first = service_router.write_kpi_snapshots(body, principal=_service())
    retry = service_router.write_kpi_snapshots(body, principal=_service())

    assert first.accepted_count == 1
    assert retry.accepted_count == 0
    assert retry.duplicate_count == 1
    assert retry.snapshots[0].id == first.snapshots[0].id
    assert retry.snapshots[0].kpi_id == definition.id
    audit = platform.list_audit("acme")[-1]
    assert audit.action == "kpi_snapshots.ingested"
    assert audit.meta["accepted_count"] == 0
    assert "value" not in audit.meta


@pytest.mark.parametrize(
    "principal",
    [
        _service(scopes=frozenset({SCOPE_READ})),
        _service(app_id="communication"),
        _service(purposes=frozenset({"kpi_read"})),
        _service(space_ids=frozenset({"sp_shared"})),
        _service(account_id="other"),
    ],
)
def test_service_batch_rejects_wrong_scope_app_purpose_space_or_account(monkeypatch, principal):
    _wire(monkeypatch)
    body = service_router.ServiceKpiSnapshotBatch(
        space_id="sp_business",
        snapshots=[service_router.ServiceKpiSnapshotItem(
            kpi_key="gross_margin",
            value=Decimal("68"),
            observed_at="2026-07-15T09:30:00Z",
            idempotency_key="key-1",
        )],
    )
    with pytest.raises(HTTPException) as denied:
        service_router.write_kpi_snapshots(body, principal=principal)
    assert denied.value.status_code == 403


def test_conflicting_service_replay_rolls_back_complete_batch(monkeypatch):
    _, store = _wire(monkeypatch)
    definition = kpi_router.create_kpi_definition(_definition_body(), principal=_human())
    first = service_router.ServiceKpiSnapshotBatch(
        space_id="sp_business",
        snapshots=[service_router.ServiceKpiSnapshotItem(
            kpi_id=definition.id,
            value=Decimal("68"),
            observed_at="2026-07-15T09:30:00Z",
            idempotency_key="same-key",
        )],
    )
    service_router.write_kpi_snapshots(first, principal=_service())
    conflict = service_router.ServiceKpiSnapshotBatch(
        space_id="sp_business",
        snapshots=[
            service_router.ServiceKpiSnapshotItem(
                kpi_id=definition.id,
                value=Decimal("99"),
                observed_at="2026-07-15T09:30:00Z",
                idempotency_key="same-key",
            ),
            service_router.ServiceKpiSnapshotItem(
                kpi_id=definition.id,
                value=Decimal("70"),
                observed_at="2026-07-15T10:30:00Z",
                idempotency_key="new-key",
            ),
        ],
    )
    with pytest.raises(HTTPException) as rejected:
        service_router.write_kpi_snapshots(conflict, principal=_service())
    assert rejected.value.status_code == 409
    assert len(store.list_snapshots(
        definition.id, account_id="acme", space_id="sp_business", limit=30,
    )) == 1
