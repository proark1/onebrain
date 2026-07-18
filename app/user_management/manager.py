"""Mission Control orchestration for sealed, signed user-management jobs."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from app.provisioning.runs import OneTimeSecretCipher
from app.user_management.base import USER_MANAGEMENT_CONTRACT, UserManagementJob
from app.user_management.crypto import (
    UserManagementCommand,
    decrypt_result,
    generate_result_keypair,
    result_aad,
    sign_command,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MissionControlUserManagement:
    def __init__(self, *, store, settings):
        self.store = store
        self.settings = settings
        self.cipher = OneTimeSecretCipher(settings)

    def create_job(self, *, deployment_id: str, action: str, payload: dict, requested_by: str) -> UserManagementJob:
        self.store.expire_and_purge(now_iso=_now().isoformat())
        if not self.settings.fleet_desired_state_private_key:
            raise RuntimeError("capability_unavailable")
        now = _now()
        ttl = 300 if action == "directory.snapshot" else 900
        private_key, public_key = generate_result_keypair()
        job = UserManagementJob(
            id=f"umj_{uuid.uuid4().hex}",
            deployment_id=deployment_id,
            action=action,
            status="queued",
            idempotency_key=uuid.uuid4().hex,
            requested_by=requested_by,
            sealed_payload=self.cipher.seal_bundle(json.dumps(payload, separators=(",", ":"), sort_keys=True)),
            sealed_result_private_key=self.cipher.seal_bundle(private_key),
            result_public_key=public_key,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=ttl)).isoformat(),
        )
        return self.store.create(job)

    def command_for(self, job: UserManagementJob) -> UserManagementCommand:
        payload = json.loads(self.cipher.open_bundle(job.sealed_payload))
        command = UserManagementCommand(
            contract=USER_MANAGEMENT_CONTRACT,
            command_id=job.id,
            deployment_id=job.deployment_id,
            action=job.action,
            idempotency_key=job.idempotency_key,
            issued_at=job.created_at,
            expires_at=job.expires_at,
            result_public_key=job.result_public_key,
            payload=payload,
        )
        return sign_command(command, self.settings.fleet_desired_state_private_key)

    def accept_result(self, job: UserManagementJob, envelope: dict[str, str]) -> UserManagementJob:
        # Validate authenticity/context before persisting a result as completed.
        result = self._decrypt(job, envelope)
        now = _now()
        ttl = 900 if job.action == "directory.snapshot" else 600
        saved = self.store.complete(
            job.id,
            job.deployment_id,
            sender_public_key=envelope["sender_public_key"],
            nonce=envelope["nonce"],
            ciphertext=envelope["ciphertext"],
            completed_at=now.isoformat(),
            result_expires_at=(now + timedelta(seconds=ttl)).isoformat(),
        )
        if not saved:
            raise ValueError("job_not_leased")
        if result.get("ok") is False:
            return self.store.fail(
                job.id,
                job.deployment_id,
                error_code=str(result.get("error_code", "internal_failure")),
                completed_at=now.isoformat(),
            ) or saved
        return saved

    def read_result(self, job: UserManagementJob) -> dict | None:
        if not job.result_ciphertext or not job.sealed_result_private_key:
            return None
        return self._decrypt(job, {
            "sender_public_key": job.result_sender_public_key,
            "nonce": job.result_nonce,
            "ciphertext": job.result_ciphertext,
        })

    def consume_secret_result(self, job_id: str) -> dict | None:
        job = self.store.consume_result(job_id, consumed_at=_now().isoformat())
        return self.read_result(job) if job else None

    def _decrypt(self, job: UserManagementJob, envelope: dict[str, str]) -> dict:
        private_key = self.cipher.open_bundle(job.sealed_result_private_key)
        return decrypt_result(
            envelope,
            private_key,
            aad=result_aad(command_id=job.id, deployment_id=job.deployment_id, action=job.action),
        )


def is_password_action(action: str) -> bool:
    return action in {"user.create", "user.password.reset"}
