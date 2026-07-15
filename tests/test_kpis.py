"""KPI domain and storage behavior."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.kpis.base import (
    KpiConflictError,
    KpiDefinition,
    KpiSnapshot,
    freshness_state,
    normalize_decimal,
    normalize_timestamp,
    threshold_state,
    validate_definition,
)
from app.kpis.memory import MemoryKpiStore


NOW = datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)


def _definition(**changes) -> KpiDefinition:
    base = KpiDefinition(
        id="kpi_margin",
        account_id="acme",
        space_id="sp_business",
        key="gross_margin",
        name="Gross margin",
        unit="%",
        freshness_minutes=60,
        warning_min=Decimal("60"),
        critical_min=Decimal("50"),
    )
    return replace(base, **changes)


def _snapshot(**changes) -> KpiSnapshot:
    base = KpiSnapshot(
        id="snap_1",
        account_id="acme",
        space_id="sp_business",
        kpi_id="kpi_margin",
        value=Decimal("68.5"),
        observed_at="2026-07-15T09:30:00+00:00",
        received_at="2026-07-15T09:31:00+00:00",
        source_ref="invoice-summary-42",
        idempotency_key="erp:gross_margin:2026-07-15T09:30Z",
        created_by="svc:key_1",
    )
    return replace(base, **changes)


def test_definition_and_decimal_validation_is_bounded():
    validate_definition(_definition())
    assert normalize_decimal("12.1234567890") == Decimal("12.1234567890")

    with pytest.raises(ValueError, match="KPI key"):
        validate_definition(_definition(key="Gross margin"))
    with pytest.raises(ValueError, match="critical_min"):
        validate_definition(_definition(critical_min=Decimal("65")))
    with pytest.raises(ValueError, match="finite"):
        normalize_decimal("NaN")
    with pytest.raises(ValueError, match="10 decimal places"):
        normalize_decimal("1.12345678901")
    with pytest.raises(ValueError, match="magnitude"):
        normalize_decimal("1e28")


def test_observation_timestamp_requires_timezone_and_rejects_future_values():
    assert normalize_timestamp("2026-07-16T11:30:00+02:00", now=NOW) == "2026-07-16T09:30:00+00:00"
    with pytest.raises(ValueError, match="timezone"):
        normalize_timestamp("2026-07-16T09:30:00", now=NOW)
    with pytest.raises(ValueError, match="five minutes"):
        normalize_timestamp("2026-07-16T10:06:00Z", now=NOW)


def test_threshold_and_freshness_states_are_deterministic():
    definition = _definition()
    assert threshold_state(definition, Decimal("68")) == "healthy"
    assert threshold_state(definition, Decimal("58")) == "warning"
    assert threshold_state(definition, Decimal("48")) == "critical"
    assert freshness_state(definition, None, now=NOW) == "awaiting_data"
    assert freshness_state(
        definition,
        _snapshot(observed_at="2026-07-16T09:30:00+00:00"),
        now=NOW,
    ) == "fresh"
    assert freshness_state(
        definition,
        _snapshot(observed_at=(NOW - timedelta(minutes=61)).isoformat()),
        now=NOW,
    ) == "stale"


def test_memory_store_persists_definitions_and_history(tmp_path):
    path = tmp_path / "kpis.json"
    store = MemoryKpiStore(str(path))
    created = store.create_definition(_definition())
    result = store.ingest_snapshots([
        _snapshot(),
        _snapshot(
            id="snap_2",
            value=Decimal("70"),
            observed_at="2026-07-15T09:45:00+00:00",
            received_at="2026-07-15T09:46:00+00:00",
            idempotency_key="erp:gross_margin:2026-07-15T09:45Z",
        ),
    ])

    assert created.created_at
    assert result.accepted_count == 2
    series = store.dashboard("acme", "sp_business", history_limit=1)
    assert len(series) == 1
    assert [point.id for point in series[0].snapshots] == ["snap_2"]

    reloaded = MemoryKpiStore(str(path))
    assert reloaded.get_definition_by_key(
        "gross_margin", account_id="acme", space_id="sp_business",
    ).id == "kpi_margin"
    assert [point.id for point in reloaded.list_snapshots(
        "kpi_margin", account_id="acme", space_id="sp_business", limit=30,
    )] == ["snap_1", "snap_2"]


def test_snapshot_ingestion_is_idempotent_and_transactional():
    store = MemoryKpiStore()
    store.create_definition(_definition())
    first = store.ingest_snapshots([_snapshot()])
    duplicate = store.ingest_snapshots([_snapshot(id="retry_id")])

    assert first.accepted_count == 1
    assert duplicate.accepted_count == 0
    assert duplicate.duplicate_count == 1
    assert duplicate.snapshots[0].id == "snap_1"

    with pytest.raises(KpiConflictError, match="conflicts"):
        store.ingest_snapshots([
            _snapshot(
                id="conflict",
                value=Decimal("99"),
            ),
            _snapshot(
                id="new_but_rolled_back",
                observed_at="2026-07-15T09:50:00+00:00",
                received_at="2026-07-15T09:51:00+00:00",
                idempotency_key="new-key",
            ),
        ])
    assert len(store.list_snapshots(
        "kpi_margin", account_id="acme", space_id="sp_business", limit=30,
    )) == 1


def test_archive_export_retention_and_deletion_stay_scoped():
    store = MemoryKpiStore()
    definition = store.create_definition(_definition())
    store.create_definition(_definition(
        id="kpi_other",
        account_id="other",
        space_id="sp_other",
        key="gross_margin",
    ))
    store.ingest_snapshots([_snapshot()])
    store.ingest_snapshots([_snapshot(
        id="snap_other",
        account_id="other",
        space_id="sp_other",
        kpi_id="kpi_other",
        idempotency_key="other-key",
    )])

    store.update_definition(replace(definition, status="archived"))
    assert store.list_definitions("acme", "sp_business") == []
    assert len(store.list_definitions("acme", "sp_business", include_archived=True)) == 1
    assert len(store.export_scope("acme")["snapshots"]) == 1

    dry = store.retention_scope(
        "acme", "sp_business", older_than="2026-07-15T09:32:00+00:00", delete=False,
    )
    assert dry == {"snapshots": 1, "snapshots_deleted": 0}
    deleted = store.retention_scope(
        "acme", "sp_business", older_than="2026-07-15T09:32:00+00:00", delete=True,
    )
    assert deleted == {"snapshots": 1, "snapshots_deleted": 1}
    assert store.delete_scope("acme") == {"definitions": 1, "snapshots": 0}
    assert len(store.export_scope("other")["definitions"]) == 1
