"""Mission Control fleet telemetry — records and store contract.

The control plane holds deployment METADATA ONLY: which deployments exist, what
versions they run, whether they are healthy, and aggregate counts. Never any
customer content — no message/document text, no titles, no names/emails, no
per-record ids, no free-text error strings, no secrets or DSNs. The heartbeat
schema (app/fleet/heartbeat.py) enforces that boundary at the ingest edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol


ALERT_KINDS = frozenset({
    "missed_heartbeat", "version_drift", "unhealthy", "migration_failed",
    "low_root_disk", "low_data_disk",
})


@dataclass(frozen=True)
class FleetKey:
    """A per-deployment bearer key a deployment uses to POST its heartbeat to
    Mission Control. Only a hash of the secret is stored (reuses service-key
    hashing). Write-only in effect: it authorizes heartbeat ingest and nothing
    else, and points AT Mission Control — it grants no access into any data plane."""
    id: str
    key_hash: str
    deployment_id: str
    label: str = ""
    status: str = "active"          # active | revoked
    created_at: str = ""
    last_used_at: str = ""


@dataclass(frozen=True)
class Heartbeat:
    """One received heartbeat. `payload` is the validated fleet.v1 body; the
    denormalized columns are what the fleet overview and watchdog read hot."""
    id: str
    deployment_id: str
    contract_version: str
    reported_at: str
    received_at: str
    healthy: bool
    version: str = ""
    migration_revision: str = ""
    payload: Dict = field(default_factory=dict)


@dataclass(frozen=True)
class FleetAlert:
    id: str
    deployment_id: str
    kind: str                       # one of ALERT_KINDS
    detail: str = ""
    status: str = "open"           # open | resolved
    created_at: str = ""
    resolved_at: str = ""


def validate_alert_kind(kind: str) -> None:
    if kind not in ALERT_KINDS:
        raise ValueError(f"Unknown alert kind: {kind}")


class FleetStore(Protocol):
    def create_key(self, key: FleetKey) -> FleetKey: ...

    def get_key(self, key_id: str) -> Optional[FleetKey]: ...

    def list_keys(self, deployment_id: str = "") -> List[FleetKey]: ...

    def revoke_key(self, key_id: str) -> bool: ...

    def touch_key(self, key_id: str, now_iso: str) -> None: ...

    def record_heartbeat(self, heartbeat: Heartbeat) -> Heartbeat: ...

    def latest_heartbeat(self, deployment_id: str) -> Optional[Heartbeat]: ...

    def latest_heartbeats(self) -> Dict[str, Heartbeat]: ...

    def list_heartbeats(self, deployment_id: str, since_iso: str = "", limit: int = 500) -> List[Heartbeat]: ...

    def prune_heartbeats(self, before_iso: str) -> int: ...

    def open_alert(self, alert: FleetAlert) -> FleetAlert: ...

    def resolve_open_alerts(self, deployment_id: str, kind: str, resolved_at: str) -> int: ...

    def list_open_alerts(self, deployment_id: str = "") -> List[FleetAlert]: ...

    def has_open_alert(self, deployment_id: str, kind: str) -> bool: ...
