"""The fleet.v1 heartbeat contract.

A deployment posts one of these to Mission Control on a timer. The schema is
CLOSED (`extra="forbid"`) and every field is a count, a version string, a flag,
or a timestamp — never customer content. If a future field would carry text,
names, ids, or errors, it does not belong here; that is the metadata-only
boundary made mechanical.
"""

from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field

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
    modules: List[ModuleReport] = Field(default_factory=list)

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
