"""Persistence-neutral user-management job and receipt contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol


USER_MANAGEMENT_CONTRACT = "user-management-command.v1"
USER_MANAGEMENT_CAPABILITY = "user_management_v1"

USER_MANAGEMENT_ACTIONS = frozenset({
    "directory.snapshot",
    "user.create",
    "user.password.reset",
    "user.disable",
    "user.enable",
    "user.delete",
})

MUTATION_ACTIONS = USER_MANAGEMENT_ACTIONS - {"directory.snapshot"}
JOB_STATUSES = frozenset({"queued", "leased", "completed", "failed", "expired"})

SAFE_ERROR_CODES = frozenset({
    "duplicate_email",
    "invalid_role",
    "invalid_location",
    "user_not_found",
    "invalid_state_transition",
    "last_active_admin",
    "ownership_reassignment_required",
    "command_expired",
    "command_replayed",
    "capability_unavailable",
    "internal_failure",
})


@dataclass(frozen=True)
class UserManagementJob:
    id: str
    deployment_id: str
    action: str
    status: str
    idempotency_key: str
    requested_by: str
    sealed_payload: str
    sealed_result_private_key: str
    result_public_key: str
    created_at: str
    expires_at: str
    leased_at: str = ""
    lease_expires_at: str = ""
    attempts: int = 0
    completed_at: str = ""
    result_sender_public_key: str = ""
    result_nonce: str = ""
    result_ciphertext: str = ""
    result_expires_at: str = ""
    result_consumed_at: str = ""
    error_code: str = ""


@dataclass(frozen=True)
class UserManagementReceipt:
    command_id: str
    action: str
    sender_public_key: str
    nonce: str
    ciphertext: str
    created_at: str
    expires_at: str


class UserManagementJobStore(Protocol):
    def create(self, job: UserManagementJob) -> UserManagementJob: ...

    def get(self, job_id: str) -> Optional[UserManagementJob]: ...

    def list_for_deployment(self, deployment_id: str, limit: int = 100) -> List[UserManagementJob]: ...

    def lease_next(self, deployment_id: str, *, now_iso: str, lease_expires_at: str) -> Optional[UserManagementJob]: ...

    def complete(
        self,
        job_id: str,
        deployment_id: str,
        *,
        sender_public_key: str,
        nonce: str,
        ciphertext: str,
        completed_at: str,
        result_expires_at: str,
    ) -> Optional[UserManagementJob]: ...

    def fail(self, job_id: str, deployment_id: str, *, error_code: str, completed_at: str) -> Optional[UserManagementJob]: ...

    def consume_result(self, job_id: str, *, consumed_at: str) -> Optional[UserManagementJob]: ...

    def expire_and_purge(self, *, now_iso: str) -> int: ...


class UserManagementReceiptStore(Protocol):
    def get(self, command_id: str) -> Optional[UserManagementReceipt]: ...

    def put(self, receipt: UserManagementReceipt) -> UserManagementReceipt: ...

    def purge(self, *, now_iso: str) -> int: ...
