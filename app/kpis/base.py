"""KPI domain records, validation, and store contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional, Protocol, Sequence


KPI_APP_ID = "kpi_dashboard"
KPI_READ_PURPOSE = "kpi_read"
KPI_CONFIGURE_PURPOSE = "kpi_configure"
KPI_SNAPSHOT_WRITE_PURPOSE = "kpi_snapshot_write"
KPI_STATUSES = frozenset({"active", "archived"})
KPI_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
MAX_DEFINITIONS_PER_SPACE = 500
MAX_ACTIVE_DEFINITIONS_PER_SPACE = 250
MAX_BATCH_SIZE = 100
MAX_HISTORY_LIMIT = 366
MAX_DECIMAL_ABS = Decimal("1e28")
MAX_DECIMAL_PLACES = 10


class KpiConflictError(ValueError):
    """A unique key or immutable snapshot conflicts with existing data."""


class KpiLimitError(ValueError):
    """A bounded KPI resource limit would be exceeded."""


@dataclass(frozen=True)
class KpiDefinition:
    id: str
    account_id: str
    space_id: str
    key: str
    name: str
    description: str = ""
    category: str = ""
    unit: str = ""
    source_label: str = ""
    owner_label: str = ""
    freshness_minutes: int = 1440
    warning_min: Optional[Decimal] = None
    warning_max: Optional[Decimal] = None
    critical_min: Optional[Decimal] = None
    critical_max: Optional[Decimal] = None
    display_order: int = 0
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class KpiSnapshot:
    id: str
    account_id: str
    space_id: str
    kpi_id: str
    value: Decimal
    observed_at: str
    received_at: str
    source_ref: str
    idempotency_key: str
    created_by: str


@dataclass(frozen=True)
class KpiSeries:
    definition: KpiDefinition
    snapshots: tuple[KpiSnapshot, ...]


@dataclass(frozen=True)
class KpiIngestResult:
    snapshots: tuple[KpiSnapshot, ...]
    accepted_count: int
    duplicate_count: int


class KpiStore(Protocol):
    def create_definition(self, definition: KpiDefinition) -> KpiDefinition: ...

    def update_definition(self, definition: KpiDefinition) -> KpiDefinition: ...

    def get_definition(
        self, kpi_id: str, *, account_id: str, space_id: str,
    ) -> Optional[KpiDefinition]: ...

    def get_definition_by_key(
        self, key: str, *, account_id: str, space_id: str,
    ) -> Optional[KpiDefinition]: ...

    def list_definitions(
        self, account_id: str, space_id: str, *, include_archived: bool = False,
    ) -> list[KpiDefinition]: ...

    def ingest_snapshots(self, snapshots: Sequence[KpiSnapshot]) -> KpiIngestResult: ...

    def list_snapshots(
        self, kpi_id: str, *, account_id: str, space_id: str, limit: int = 30,
    ) -> list[KpiSnapshot]: ...

    def dashboard(
        self, account_id: str, space_id: str, *, history_limit: int = 30,
        include_archived: bool = False,
    ) -> list[KpiSeries]: ...

    def export_scope(self, account_id: str, space_id: str = "") -> dict: ...

    def delete_scope(self, account_id: str, space_id: str = "") -> dict[str, int]: ...

    def retention_scope(
        self, account_id: str, space_id: str, *, older_than: str, delete: bool,
    ) -> dict[str, int]: ...


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_decimal(value) -> Decimal:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("KPI value must be a decimal number.") from exc
    if not decimal_value.is_finite():
        raise ValueError("KPI value must be finite.")
    if abs(decimal_value) >= MAX_DECIMAL_ABS:
        raise ValueError("KPI value exceeds NUMERIC(38,10) magnitude.")
    exponent = decimal_value.as_tuple().exponent
    if exponent < -MAX_DECIMAL_PLACES:
        raise ValueError(f"KPI value may have at most {MAX_DECIMAL_PLACES} decimal places.")
    return decimal_value


def normalize_optional_decimal(value) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    return normalize_decimal(value)


def normalize_timestamp(value: str, *, now: Optional[datetime] = None) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("observed_at is required.")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at must include a timezone.")
    utc_value = parsed.astimezone(timezone.utc)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if utc_value > current + timedelta(minutes=5):
        raise ValueError("observed_at cannot be more than five minutes in the future.")
    return utc_value.isoformat()


def validate_definition(definition: KpiDefinition) -> None:
    if not definition.id.strip() or not definition.account_id.strip() or not definition.space_id.strip():
        raise ValueError("KPI id, account_id, and space_id are required.")
    if not KPI_KEY_RE.fullmatch(definition.key):
        raise ValueError("KPI key must be lowercase letters, digits, or underscores and start with a letter.")
    _validate_text("name", definition.name, 1, 120)
    _validate_text("description", definition.description, 0, 500)
    _validate_text("category", definition.category, 0, 80)
    _validate_text("unit", definition.unit, 0, 32)
    _validate_text("source_label", definition.source_label, 0, 120)
    _validate_text("owner_label", definition.owner_label, 0, 120)
    if not 1 <= definition.freshness_minutes <= 525_600:
        raise ValueError("freshness_minutes must be between 1 and 525600.")
    if not -1_000_000 <= definition.display_order <= 1_000_000:
        raise ValueError("display_order is outside the supported range.")
    if definition.status not in KPI_STATUSES:
        raise ValueError("KPI status must be active or archived.")
    thresholds = (
        definition.warning_min, definition.warning_max,
        definition.critical_min, definition.critical_max,
    )
    for threshold in thresholds:
        if threshold is not None:
            normalize_decimal(threshold)
    if (
        definition.critical_min is not None
        and definition.warning_min is not None
        and definition.critical_min > definition.warning_min
    ):
        raise ValueError("critical_min must be less than or equal to warning_min.")
    if (
        definition.warning_max is not None
        and definition.critical_max is not None
        and definition.warning_max > definition.critical_max
    ):
        raise ValueError("warning_max must be less than or equal to critical_max.")


def validate_snapshot(snapshot: KpiSnapshot) -> None:
    if not snapshot.id.strip() or not snapshot.account_id.strip() or not snapshot.space_id.strip():
        raise ValueError("Snapshot id, account_id, and space_id are required.")
    if not snapshot.kpi_id.strip():
        raise ValueError("kpi_id is required.")
    normalize_decimal(snapshot.value)
    normalize_timestamp(snapshot.observed_at)
    _parse_server_timestamp(snapshot.received_at, "received_at")
    _validate_text("source_ref", snapshot.source_ref, 0, 200)
    _validate_text("idempotency_key", snapshot.idempotency_key, 1, 128)
    _validate_text("created_by", snapshot.created_by, 1, 200)


def threshold_state(definition: KpiDefinition, value: Decimal) -> str:
    value = normalize_decimal(value)
    if (
        (definition.critical_min is not None and value < definition.critical_min)
        or (definition.critical_max is not None and value > definition.critical_max)
    ):
        return "critical"
    if (
        (definition.warning_min is not None and value < definition.warning_min)
        or (definition.warning_max is not None and value > definition.warning_max)
    ):
        return "warning"
    return "healthy"


def freshness_state(
    definition: KpiDefinition,
    latest: Optional[KpiSnapshot],
    *,
    now: Optional[datetime] = None,
) -> str:
    if latest is None:
        return "awaiting_data"
    observed = _parse_server_timestamp(latest.observed_at, "observed_at")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return "stale" if current - observed > timedelta(minutes=definition.freshness_minutes) else "fresh"


def snapshot_semantically_equal(left: KpiSnapshot, right: KpiSnapshot) -> bool:
    return (
        left.account_id == right.account_id
        and left.space_id == right.space_id
        and left.kpi_id == right.kpi_id
        and normalize_decimal(left.value) == normalize_decimal(right.value)
        and normalize_timestamp(left.observed_at) == normalize_timestamp(right.observed_at)
        and left.source_ref == right.source_ref
    )


def bounded_history_limit(limit: int) -> int:
    return min(max(int(limit), 1), MAX_HISTORY_LIMIT)


def _validate_text(field: str, value: str, minimum: int, maximum: int) -> None:
    length = len(value or "")
    if length < minimum or length > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum} characters.")


def _parse_server_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone.")
    return parsed.astimezone(timezone.utc)
