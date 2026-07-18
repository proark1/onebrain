"""The fleet.v1 + fleet.v2 heartbeat contracts.

A deployment posts one of these to Mission Control on a timer. Every schema is
CLOSED (`extra="forbid"`) and every field is a count, a version string, an
enum, a flag, or a timestamp — never customer content. If a future field would
carry text, names, ids, or errors, it does not belong here; that is the
metadata-only boundary made mechanical.
"""

from __future__ import annotations

from typing import Annotated, List, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT_VERSION = "fleet.v1"


class OneBrainReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(default="", max_length=64)
    migration_revision: str = Field(default="", max_length=64)
    healthy: bool = True
    # Aggregate counts only — the size of things, never the things.
    chunks: int = Field(default=0, ge=0)
    intake_records: int = Field(default=0, ge=0)
    users: int = Field(default=0, ge=0)
    accounts: int = Field(default=0, ge=0)
    active_service_keys: int = Field(default=0, ge=0)
    jobs_pending: int = Field(default=0, ge=0)
    jobs_failed: int = Field(default=0, ge=0)
    auth_failures_recent: int = Field(default=0, ge=0)
    api_5xx_recent: int = Field(default=0, ge=0)


class ModuleReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module_id: str = Field(max_length=64)
    version: str = Field(default="", max_length=64)
    healthy: bool = True
    events_pending: int = Field(default=0, ge=0)
    events_failed: int = Field(default=0, ge=0)


class FleetHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["fleet.v1"] = CONTRACT_VERSION
    deployment_id: str = Field(min_length=1, max_length=120)
    reported_at: str = Field(min_length=1, max_length=40)
    onebrain: OneBrainReport
    modules: List[ModuleReport] = Field(default_factory=list, max_length=50)

    @property
    def healthy(self) -> bool:
        return self.onebrain.healthy and all(module.healthy for module in self.modules)


def build_heartbeat(
    *,
    deployment_id: str,
    reported_at: str,
    version: str = "",
    migration_revision: str = "",
    onebrain_healthy: bool = True,
    chunks: int = 0,
    intake_records: int = 0,
    users: int = 0,
    accounts: int = 0,
    active_service_keys: int = 0,
    jobs_pending: int = 0,
    jobs_failed: int = 0,
    auth_failures_recent: int = 0,
    api_5xx_recent: int = 0,
    modules: List[ModuleReport] | None = None,
) -> FleetHeartbeat:
    """Assemble a heartbeat from primitive local counts. Pure — no I/O — so a
    deployment's reporter builds it from its own observability numbers and the
    result is unit-testable."""
    return FleetHeartbeat(
        deployment_id=deployment_id,
        reported_at=reported_at,
        onebrain=OneBrainReport(
            version=version,
            migration_revision=migration_revision,
            healthy=onebrain_healthy,
            chunks=chunks,
            intake_records=intake_records,
            users=users,
            accounts=accounts,
            active_service_keys=active_service_keys,
            jobs_pending=jobs_pending,
            jobs_failed=jobs_failed,
            auth_failures_recent=auth_failures_recent,
            api_5xx_recent=api_5xx_recent,
        ),
        modules=modules or [],
    )


# --- fleet.v2 ------------------------------------------------------------------

CONTRACT_VERSION_V2 = "fleet.v2"
UPDATE_OUTCOMES = ("none", "in_progress", "succeeded", "failed", "rolled_back")


class UpdateReport(BaseModel):
    """Bounded update-outcome (architecture §3f): lets MC distinguish converged /
    failed-and-rolled-back / not-yet-started, which version+healthy cannot.
    attempt_id is the RolloutRun id MC offered (opaque token, never free text);
    ts is when the box recorded the outcome. Field semantics are LOAD-BEARING for
    the P2 reconcile tick — do not repurpose."""
    model_config = ConfigDict(extra="forbid")

    last_target_version: str = Field(default="", max_length=64)
    outcome: Literal["none", "in_progress", "succeeded", "failed", "rolled_back"] = "none"
    migration_reached: str = Field(default="", max_length=64)
    attempt_id: str = Field(default="", max_length=64)
    ts: str = Field(default="", max_length=40)
    # Reserved NOW so P3's on-box backups need no fleet.v3 (C3): boxes hold a
    # heartbeat-only fleet key and cannot call the operator record_backup
    # endpoint, and every model here is extra="forbid" — without these slots,
    # the plan gate's BackupRun requirement would permanently block
    # migration-crossing rollouts to pull-managed boxes. Metadata-only,
    # defaults inert ('' = no claim). P2's reconcile tick materializes
    # BackupRun rows from these fields.
    backup_status: Literal["", "success", "failed"] = ""
    backup_ts: str = Field(default="", max_length=40)
    # G1-3 convergence signal (P5-02): the secrets_epoch the box last SUCCESSFULLY
    # applied (wrote /opt/onebrain/.env for). The operator watches this in /overview
    # to gate a wrapper-key private-key swap on "every box echoes the new epoch" —
    # without it the swap is blind, which is exactly what makes the G1-1 fleet-wide
    # brick likely in practice. Additive: old boxes omit it -> default 0 (extra=
    # "forbid" tolerates a MISSING field, only rejects UNKNOWN ones). MC-first
    # dual-version ordering (G3-6): MC ingests this before any box emits it.
    applied_secrets_epoch: int = Field(default=0, ge=0)
    # 7d/A17 (P5-07): a metadata-only backup manifest — "sha256:<64hex>:<bytes>" of the
    # ENCRYPTED backup object (a digest + size; NEVER a path, name, or content). Lets MC's
    # pull reconcile gate a migration-crossing rollout on a WELL-FORMED manifest instead of
    # a bare self-reported backup_status="success", so a phantom-backup box can no longer
    # disable its own restore net by asserting a naked success. Additive: old boxes omit it
    # -> default "" (extra="forbid" tolerates a MISSING field). Ships alongside
    # applied_secrets_epoch under the SAME MC-first dual-version ordering (G3-6). RESIDUAL:
    # the box still AUTHORS the hash, so a fully-compromised box can fabricate a well-formed
    # one — this raises the bar from "any empty assertion" to "a well-formed manifest";
    # full closure (an off-box backup-object read echoed by MC) is a §6 ops item.
    backup_manifest: str = Field(default="", max_length=128)


class OneBrainReportV2(OneBrainReport):
    """v1 counts plus uptime: uptime_seconds lets MC tell 'restarted' (cumulative
    counters reset) from 'recovered' without changing any v1 key's meaning."""
    uptime_seconds: int = Field(default=0, ge=0)


class StorageCapacityReport(BaseModel):
    """Capacity for one host filesystem, expressed only as bounded metadata.

    ``0/0`` means that this reporter cannot observe the filesystem.  That is
    deliberately distinct from a full disk and lets Mission Control continue
    accepting fleet.v2 heartbeats from older boxes until their host reporter is
    refreshed.
    """
    model_config = ConfigDict(extra="forbid")

    total_bytes: int = Field(default=0, ge=0)
    available_bytes: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _available_cannot_exceed_total(self):
        if self.available_bytes > self.total_bytes:
            raise ValueError("available_bytes cannot exceed total_bytes")
        return self


class StorageReport(BaseModel):
    """Metadata-only capacity plus a concrete durable-volume availability signal."""
    model_config = ConfigDict(extra="forbid")

    root: StorageCapacityReport = Field(default_factory=StorageCapacityReport)
    data: StorageCapacityReport = Field(default_factory=StorageCapacityReport)
    # ``True`` means the host's UUID/mount verifier rejected or could not see
    # the persistent data volume. It is deliberately distinct from unknown
    # legacy 0/0 capacity so the watchdog can open a concrete operator alert.
    data_volume_unavailable: bool = False


class FleetHeartbeatV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["fleet.v2"] = CONTRACT_VERSION_V2
    deployment_id: str = Field(min_length=1, max_length=120)
    reported_at: str = Field(min_length=1, max_length=40)
    onebrain: OneBrainReportV2
    modules: List[ModuleReport] = Field(default_factory=list, max_length=50)
    update: UpdateReport = Field(default_factory=UpdateReport)
    # Additive fleet.v2 field. Older boxes omit it and correctly report unknown
    # capacity (0/0); the watchdog never turns an unknown volume into an alert.
    storage: StorageReport = Field(default_factory=StorageReport)

    @property
    def healthy(self) -> bool:
        return self.onebrain.healthy and all(module.healthy for module in self.modules)


AnyFleetHeartbeat = Annotated[
    Union[FleetHeartbeat, FleetHeartbeatV2], Field(discriminator="contract_version")
]
# ACCEPTED CONTRACT TIGHTENING (A2): the discriminated union requires the
# contract_version key to be PRESENT — a body omitting it now 422s, where the
# bare-FleetHeartbeat body type let it default to fleet.v1. Every real reporter
# emits the key (model_dump includes defaults), so live fleet traffic is
# unaffected; only a hand-rolled client relying on the default breaks. Ground
# rule 1's "v1 must still ingest" means a v1 body CARRYING
# contract_version="fleet.v1". Pinned by test_missing_discriminator_is_rejected.


def build_heartbeat_v2(
    *,
    deployment_id: str,
    reported_at: str,
    version: str = "",
    migration_revision: str = "",
    onebrain_healthy: bool = True,
    chunks: int = 0,
    intake_records: int = 0,
    users: int = 0,
    accounts: int = 0,
    active_service_keys: int = 0,
    jobs_pending: int = 0,
    jobs_failed: int = 0,
    auth_failures_recent: int = 0,
    api_5xx_recent: int = 0,
    uptime_seconds: int = 0,
    modules: List[ModuleReport] | None = None,
    update: UpdateReport | None = None,
    storage: StorageReport | None = None,
) -> FleetHeartbeatV2:
    """Pure v2 assembler mirroring build_heartbeat (which stays for v1 tests)."""
    return FleetHeartbeatV2(
        deployment_id=deployment_id,
        reported_at=reported_at,
        onebrain=OneBrainReportV2(
            version=version,
            migration_revision=migration_revision,
            healthy=onebrain_healthy,
            chunks=chunks,
            intake_records=intake_records,
            users=users,
            accounts=accounts,
            active_service_keys=active_service_keys,
            jobs_pending=jobs_pending,
            jobs_failed=jobs_failed,
            auth_failures_recent=auth_failures_recent,
            api_5xx_recent=api_5xx_recent,
            uptime_seconds=uptime_seconds,
        ),
        modules=modules or [],
        update=update or UpdateReport(),
        storage=storage or StorageReport(),
    )
