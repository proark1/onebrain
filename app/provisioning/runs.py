"""Provisioning-run state, dispatch, callbacks, and one-time secrets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings
from app.db.schema import validate_postgres_schema


STATUS_PENDING = "pending"
STATUS_DISPATCH_FAILED = "dispatch_failed"
STATUS_DISPATCHED = "dispatched"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

PROVISIONING_STATUSES = frozenset({
    STATUS_PENDING,
    STATUS_DISPATCH_FAILED,
    STATUS_DISPATCHED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_CANCELLED,
})
TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED, STATUS_DISPATCH_FAILED})
STATUS_RANK = {
    STATUS_PENDING: 0,
    STATUS_DISPATCH_FAILED: 1,
    STATUS_DISPATCHED: 2,
    STATUS_RUNNING: 3,
    STATUS_FAILED: 4,
    STATUS_CANCELLED: 4,
    STATUS_SUCCEEDED: 5,
}


@dataclass(frozen=True)
class ProvisioningRun:
    id: str
    account_id: str
    deployment_id: str
    bundle_id: str
    requested_by: str
    status: str = STATUS_PENDING
    external_provider: str = "github_actions"
    external_run_id: str = ""
    external_run_url: str = ""
    request_payload: Dict = None
    result_payload: Dict = None
    railway_project_id: str = ""
    railway_environment_id: str = ""
    service_urls: Dict[str, str] = None
    migration_revision: str = ""
    smoke_status: str = ""
    failure_reason: str = ""
    bootstrap_secret_id: str = ""
    retry_of_run_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    dispatched_at: str = ""
    completed_at: str = ""

    def __post_init__(self):
        object.__setattr__(self, "request_payload", dict(self.request_payload or {}))
        object.__setattr__(self, "result_payload", dict(self.result_payload or {}))
        object.__setattr__(self, "service_urls", dict(self.service_urls or {}))


@dataclass(frozen=True)
class OneTimeSecretEnvelope:
    id: str
    purpose: str
    account_id: str
    deployment_id: str
    ciphertext: str
    nonce: str
    key_version: str
    expires_at: str
    read_at: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class ProvisioningCallback:
    status: str
    external_run_id: str = ""
    external_run_url: str = ""
    result_payload: Dict = None
    railway_project_id: str = ""
    railway_environment_id: str = ""
    service_urls: Dict[str, str] = None
    migration_revision: str = ""
    smoke_status: str = ""
    failure_reason: str = ""
    bootstrap_password: str = ""

    def __post_init__(self):
        object.__setattr__(self, "result_payload", dict(self.result_payload or {}))
        object.__setattr__(self, "service_urls", dict(self.service_urls or {}))


class ProvisioningRunStore(Protocol):
    def create_run(self, run: ProvisioningRun) -> ProvisioningRun: ...

    def get_run(self, run_id: str) -> Optional[ProvisioningRun]: ...

    def list_runs(self, account_id: str = "", deployment_id: str = "") -> List[ProvisioningRun]: ...

    def update_run(self, run: ProvisioningRun) -> ProvisioningRun: ...

    def create_secret(self, envelope: OneTimeSecretEnvelope) -> OneTimeSecretEnvelope: ...

    def get_secret(self, secret_id: str) -> Optional[OneTimeSecretEnvelope]: ...

    def mark_secret_read(self, secret_id: str) -> OneTimeSecretEnvelope: ...


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value) -> str:
    return value.isoformat() if value else ""


def _json(value) -> Dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        return json.loads(value or "{}")
    return dict(value)


def _validate_status(status: str) -> None:
    if status not in PROVISIONING_STATUSES:
        raise ValueError(f"Unknown provisioning status: {status}")


def _hashed_secret(secret: str) -> str:
    return "sha256$" + hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_callback_secret(secret: str, stored_hash: str) -> bool:
    try:
        algo, digest = stored_hash.split("$", 1)
    except ValueError:
        return False
    if algo != "sha256":
        return False
    candidate = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return hmac.compare_digest(candidate, digest)


def hash_callback_secret(secret: str) -> str:
    return _hashed_secret(secret)


def _fernet_key(raw: str) -> bytes:
    value = (raw or "").strip()
    if not value:
        raise ValueError("ONEBRAIN_SECRET_ENCRYPTION_KEY is required.")
    try:
        decoded = base64.urlsafe_b64decode(value.encode("utf-8"))
        if len(decoded) == 32:
            return value.encode("utf-8")
    except Exception:
        pass
    try:
        decoded = bytes.fromhex(value)
        if len(decoded) == 32:
            return base64.urlsafe_b64encode(decoded)
    except Exception:
        pass
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class OneTimeSecretCipher:
    def __init__(self, settings: Settings):
        self._fernet = Fernet(_fernet_key(settings.secret_encryption_key))
        self.key_version = settings.secret_encryption_key_version or "v1"
        self.ttl_seconds = max(1, int(settings.bootstrap_secret_ttl_seconds or 3600))

    def envelope(
        self,
        *,
        purpose: str,
        account_id: str,
        deployment_id: str,
        plaintext: str,
    ) -> OneTimeSecretEnvelope:
        expires = datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)
        return OneTimeSecretEnvelope(
            id=f"ots_{uuid4().hex}",
            purpose=purpose,
            account_id=account_id,
            deployment_id=deployment_id,
            ciphertext=self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8"),
            nonce="",
            key_version=self.key_version,
            expires_at=expires.isoformat(),
        )

    def decrypt(self, envelope: OneTimeSecretEnvelope) -> str:
        if envelope.read_at:
            raise ValueError("Secret has already been read.")
        if _parse_iso(envelope.expires_at) < datetime.now(timezone.utc):
            raise ValueError("Secret has expired.")
        try:
            return self._fernet.decrypt(envelope.ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Secret could not be decrypted.") from exc


class MemoryProvisioningRunStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._runs: Dict[str, ProvisioningRun] = {}
        self._secrets: Dict[str, OneTimeSecretEnvelope] = {}
        self._persist_path = persist_path
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not (self._persist_path and os.path.exists(self._persist_path)):
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._runs = {row["id"]: ProvisioningRun(**row) for row in data.get("runs", [])}
            self._secrets = {row["id"]: OneTimeSecretEnvelope(**row) for row in data.get("secrets", [])}
        except Exception:
            self._runs, self._secrets = {}, {}

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        with open(self._persist_path, "w", encoding="utf-8") as fh:
            json.dump({
                "runs": [asdict(run) for run in self._runs.values()],
                "secrets": [asdict(secret) for secret in self._secrets.values()],
            }, fh)

    def create_run(self, run: ProvisioningRun) -> ProvisioningRun:
        _validate_status(run.status)
        with self._lock:
            if run.id in self._runs:
                raise ValueError(f"provisioning run already exists: {run.id}")
            stamped = replace(run, created_at=run.created_at or now_iso(), updated_at=run.updated_at or now_iso())
            self._runs[run.id] = stamped
            self._save()
            return stamped

    def get_run(self, run_id: str) -> Optional[ProvisioningRun]:
        return self._runs.get(run_id)

    def list_runs(self, account_id: str = "", deployment_id: str = "") -> List[ProvisioningRun]:
        runs = self._runs.values()
        if account_id:
            runs = [run for run in runs if run.account_id == account_id]
        if deployment_id:
            runs = [run for run in runs if run.deployment_id == deployment_id]
        return sorted(runs, key=lambda run: run.created_at or run.id, reverse=True)

    def update_run(self, run: ProvisioningRun) -> ProvisioningRun:
        _validate_status(run.status)
        with self._lock:
            if run.id not in self._runs:
                raise ValueError(f"unknown provisioning run: {run.id}")
            updated = replace(run, updated_at=now_iso())
            self._runs[run.id] = updated
            self._save()
            return updated

    def create_secret(self, envelope: OneTimeSecretEnvelope) -> OneTimeSecretEnvelope:
        with self._lock:
            if envelope.id in self._secrets:
                raise ValueError(f"one-time secret already exists: {envelope.id}")
            stamped = replace(envelope, created_at=envelope.created_at or now_iso())
            self._secrets[envelope.id] = stamped
            self._save()
            return stamped

    def get_secret(self, secret_id: str) -> Optional[OneTimeSecretEnvelope]:
        return self._secrets.get(secret_id)

    def mark_secret_read(self, secret_id: str) -> OneTimeSecretEnvelope:
        with self._lock:
            envelope = self._secrets.get(secret_id)
            if not envelope:
                raise ValueError(f"unknown one-time secret: {secret_id}")
            if envelope.read_at:
                raise ValueError("Secret has already been read.")
            updated = replace(envelope, read_at=now_iso())
            self._secrets[secret_id] = updated
            self._save()
            return updated


class PostgresProvisioningRunStore:
    def __init__(self, dsn: str):
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._validate_schema()

    def _conn(self):
        return self._psycopg.connect(self._dsn)

    def _validate_schema(self) -> None:
        with self._conn() as conn:
            validate_postgres_schema(conn, ("provisioning_runs", "one_time_secret_envelopes"))

    def create_run(self, run: ProvisioningRun) -> ProvisioningRun:
        _validate_status(run.status)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO provisioning_runs
                (id, account_id, deployment_id, bundle_id, requested_by, status, external_provider,
                 external_run_id, external_run_url, request_payload, result_payload,
                 railway_project_id, railway_environment_id, service_urls, migration_revision,
                 smoke_status, failure_reason, bootstrap_secret_id, retry_of_run_id,
                 dispatched_at, completed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, account_id, deployment_id, bundle_id, requested_by, status,
                    external_provider, external_run_id, external_run_url, request_payload,
                    result_payload, railway_project_id, railway_environment_id, service_urls,
                    migration_revision, smoke_status, failure_reason, bootstrap_secret_id,
                    retry_of_run_id, created_at, updated_at, dispatched_at, completed_at
                """,
                self._run_params(run),
            )
            row = cur.fetchone()
            conn.commit()
        return self._run(row)

    def get_run(self, run_id: str) -> Optional[ProvisioningRun]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._run_cols()} FROM provisioning_runs WHERE id = %s", (run_id,))
            row = cur.fetchone()
        return self._run(row) if row else None

    def list_runs(self, account_id: str = "", deployment_id: str = "") -> List[ProvisioningRun]:
        filters = []
        params = []
        if account_id:
            filters.append("account_id = %s")
            params.append(account_id)
        if deployment_id:
            filters.append("deployment_id = %s")
            params.append(deployment_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._run_cols()} FROM provisioning_runs {where} ORDER BY created_at DESC, id DESC",
                tuple(params),
            )
            rows = cur.fetchall()
        return [self._run(row) for row in rows]

    def update_run(self, run: ProvisioningRun) -> ProvisioningRun:
        _validate_status(run.status)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE provisioning_runs
                SET status = %s,
                    external_run_id = %s,
                    external_run_url = %s,
                    result_payload = %s,
                    railway_project_id = %s,
                    railway_environment_id = %s,
                    service_urls = %s,
                    migration_revision = %s,
                    smoke_status = %s,
                    failure_reason = %s,
                    bootstrap_secret_id = %s,
                    updated_at = now(),
                    dispatched_at = %s,
                    completed_at = %s
                WHERE id = %s
                RETURNING id, account_id, deployment_id, bundle_id, requested_by, status,
                    external_provider, external_run_id, external_run_url, request_payload,
                    result_payload, railway_project_id, railway_environment_id, service_urls,
                    migration_revision, smoke_status, failure_reason, bootstrap_secret_id,
                    retry_of_run_id, created_at, updated_at, dispatched_at, completed_at
                """,
                (
                    run.status,
                    run.external_run_id,
                    run.external_run_url,
                    json.dumps(run.result_payload),
                    run.railway_project_id,
                    run.railway_environment_id,
                    json.dumps(run.service_urls),
                    run.migration_revision,
                    run.smoke_status,
                    run.failure_reason[:1000],
                    run.bootstrap_secret_id,
                    _parse_iso(run.dispatched_at) if run.dispatched_at else None,
                    _parse_iso(run.completed_at) if run.completed_at else None,
                    run.id,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        if not row:
            raise ValueError(f"unknown provisioning run: {run.id}")
        return self._run(row)

    def create_secret(self, envelope: OneTimeSecretEnvelope) -> OneTimeSecretEnvelope:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO one_time_secret_envelopes
                (id, purpose, account_id, deployment_id, ciphertext, nonce, key_version, expires_at, read_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, purpose, account_id, deployment_id, ciphertext, nonce, key_version,
                    expires_at, read_at, created_at
                """,
                (
                    envelope.id,
                    envelope.purpose,
                    envelope.account_id,
                    envelope.deployment_id,
                    envelope.ciphertext,
                    envelope.nonce,
                    envelope.key_version,
                    _parse_iso(envelope.expires_at),
                    _parse_iso(envelope.read_at) if envelope.read_at else None,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return self._secret(row)

    def get_secret(self, secret_id: str) -> Optional[OneTimeSecretEnvelope]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, purpose, account_id, deployment_id, ciphertext, nonce, key_version,
                    expires_at, read_at, created_at
                FROM one_time_secret_envelopes
                WHERE id = %s
                """,
                (secret_id,),
            )
            row = cur.fetchone()
        return self._secret(row) if row else None

    def mark_secret_read(self, secret_id: str) -> OneTimeSecretEnvelope:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE one_time_secret_envelopes
                SET read_at = now()
                WHERE id = %s AND read_at IS NULL
                RETURNING id, purpose, account_id, deployment_id, ciphertext, nonce, key_version,
                    expires_at, read_at, created_at
                """,
                (secret_id,),
            )
            row = cur.fetchone()
            conn.commit()
        if not row:
            raise ValueError("Secret has already been read or does not exist.")
        return self._secret(row)

    def _run_cols(self) -> str:
        return (
            "id, account_id, deployment_id, bundle_id, requested_by, status, external_provider, "
            "external_run_id, external_run_url, request_payload, result_payload, railway_project_id, "
            "railway_environment_id, service_urls, migration_revision, smoke_status, failure_reason, "
            "bootstrap_secret_id, retry_of_run_id, created_at, updated_at, dispatched_at, completed_at"
        )

    def _run_params(self, run: ProvisioningRun) -> tuple:
        return (
            run.id,
            run.account_id,
            run.deployment_id,
            run.bundle_id,
            run.requested_by,
            run.status,
            run.external_provider,
            run.external_run_id,
            run.external_run_url,
            json.dumps(run.request_payload),
            json.dumps(run.result_payload),
            run.railway_project_id,
            run.railway_environment_id,
            json.dumps(run.service_urls),
            run.migration_revision,
            run.smoke_status,
            run.failure_reason,
            run.bootstrap_secret_id,
            run.retry_of_run_id,
            _parse_iso(run.dispatched_at) if run.dispatched_at else None,
            _parse_iso(run.completed_at) if run.completed_at else None,
        )

    def _run(self, row) -> ProvisioningRun:
        return ProvisioningRun(
            id=row[0],
            account_id=row[1],
            deployment_id=row[2],
            bundle_id=row[3],
            requested_by=row[4],
            status=row[5],
            external_provider=row[6],
            external_run_id=row[7],
            external_run_url=row[8],
            request_payload=_json(row[9]),
            result_payload=_json(row[10]),
            railway_project_id=row[11],
            railway_environment_id=row[12],
            service_urls=_json(row[13]),
            migration_revision=row[14],
            smoke_status=row[15],
            failure_reason=row[16],
            bootstrap_secret_id=row[17],
            retry_of_run_id=row[18],
            created_at=_iso(row[19]),
            updated_at=_iso(row[20]),
            dispatched_at=_iso(row[21]),
            completed_at=_iso(row[22]),
        )

    def _secret(self, row) -> OneTimeSecretEnvelope:
        return OneTimeSecretEnvelope(
            id=row[0],
            purpose=row[1],
            account_id=row[2],
            deployment_id=row[3],
            ciphertext=row[4],
            nonce=row[5],
            key_version=row[6],
            expires_at=_iso(row[7]),
            read_at=_iso(row[8]),
            created_at=_iso(row[9]),
        )


class GitHubWorkflowDispatcher:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return all([
            self.settings.github_owner,
            self.settings.github_repo,
            self.settings.github_workflow,
            self.settings.github_ref,
            self.settings.github_dispatch_token,
        ])

    def dispatch(self, run: ProvisioningRun) -> ProvisioningRun:
        if not self.enabled:
            raise RuntimeError("GitHub provisioning dispatch is not configured.")
        callback_url = str(run.request_payload.get("callback_url", "")).strip()
        if not callback_url:
            raise RuntimeError("Provisioning callback URL is required.")
        callback_url = callback_url.replace("{run_id}", run.id)
        url = (
            f"https://api.github.com/repos/{self.settings.github_owner}/"
            f"{self.settings.github_repo}/actions/workflows/"
            f"{self.settings.github_workflow}/dispatches"
        )
        payload = {
            "ref": self.settings.github_ref,
            "inputs": {
                "run_id": run.id,
                "account_id": run.account_id,
                "deployment_id": run.deployment_id,
                "bundle_id": run.bundle_id,
                "customer_name": str(run.request_payload.get("customer_name", "")),
                "deployment_type": str(run.request_payload.get("deployment_type", "")),
                "region": str(run.request_payload.get("region", "")),
                "release_ring": str(run.request_payload.get("release_ring", "")),
                "initial_version": str(run.request_payload.get("initial_version", "")),
                "module_versions_json": json.dumps(run.request_payload.get("module_versions", {})),
                "brand_theme_json": json.dumps(run.request_payload.get("brand_theme", {})),
                "callback_url": callback_url,
                "callback_key_id": self.settings.provisioning_callback_key_id,
                "dry_run": "true" if run.request_payload.get("dry_run", True) else "false",
            },
        }
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.settings.github_dispatch_token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=20):
                pass
        except HTTPError as exc:
            body = exc.read().decode("utf-8")[:500]
            raise RuntimeError(f"GitHub workflow dispatch failed ({exc.code}): {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"GitHub workflow dispatch failed: {exc.reason}") from exc

        workflow_url = (
            f"https://github.com/{self.settings.github_owner}/{self.settings.github_repo}/"
            f"actions/workflows/{self.settings.github_workflow}"
        )
        return replace(run, status=STATUS_DISPATCHED, external_run_url=workflow_url, dispatched_at=now_iso())


def create_run(
    store: ProvisioningRunStore,
    *,
    account_id: str,
    deployment_id: str,
    bundle_id: str,
    requested_by: str,
    payload: Dict,
    retry_of_run_id: str = "",
) -> ProvisioningRun:
    return store.create_run(ProvisioningRun(
        id=f"prun_{uuid4().hex}",
        account_id=account_id,
        deployment_id=deployment_id,
        bundle_id=bundle_id,
        requested_by=requested_by,
        request_payload=payload,
        retry_of_run_id=retry_of_run_id,
    ))


def mark_dispatch_failed(store: ProvisioningRunStore, run: ProvisioningRun, reason: str) -> ProvisioningRun:
    return store.update_run(replace(
        run,
        status=STATUS_DISPATCH_FAILED,
        failure_reason=reason[:1000],
        completed_at=now_iso(),
    ))


def apply_callback(
    store: ProvisioningRunStore,
    settings: Settings,
    run_id: str,
    callback: ProvisioningCallback,
) -> ProvisioningRun:
    _validate_status(callback.status)
    run = store.get_run(run_id)
    if not run:
        raise KeyError(f"unknown provisioning run: {run_id}")
    # A terminal run is immutable: reject ANY further callback, not just a status
    # change. The old "status != run.status" guard let a replayed succeeded->
    # succeeded callback re-encrypt and overwrite the bootstrap secret with an
    # attacker-chosen password. Duplicate callbacks are the caller's concern.
    if run.status in TERMINAL_STATUSES:
        raise ValueError("terminal provisioning run cannot be modified")
    if STATUS_RANK[callback.status] < STATUS_RANK[run.status]:
        raise ValueError("stale provisioning callback cannot move status backward")

    bootstrap_secret_id = run.bootstrap_secret_id
    failure_reason = callback.failure_reason[:1000]
    status = callback.status
    if callback.bootstrap_password:
        if status != STATUS_SUCCEEDED:
            raise ValueError("bootstrap_password is accepted only with succeeded callbacks")
        try:
            cipher = OneTimeSecretCipher(settings)
            envelope = cipher.envelope(
                purpose="bootstrap_admin_password",
                account_id=run.account_id,
                deployment_id=run.deployment_id,
                plaintext=callback.bootstrap_password,
            )
            bootstrap_secret_id = store.create_secret(envelope).id
        except Exception as exc:
            status = STATUS_FAILED
            failure_reason = "bootstrap_secret_encryption_failed"
            bootstrap_secret_id = ""

    completed_at = run.completed_at
    if status in TERMINAL_STATUSES:
        completed_at = completed_at or now_iso()

    return store.update_run(replace(
        run,
        status=status,
        external_run_id=callback.external_run_id or run.external_run_id,
        external_run_url=callback.external_run_url or run.external_run_url,
        result_payload=callback.result_payload,
        railway_project_id=callback.railway_project_id,
        railway_environment_id=callback.railway_environment_id,
        service_urls=callback.service_urls,
        migration_revision=callback.migration_revision,
        smoke_status=callback.smoke_status,
        failure_reason=failure_reason,
        bootstrap_secret_id=bootstrap_secret_id,
        completed_at=completed_at,
    ))


def read_one_time_secret(
    store: ProvisioningRunStore,
    settings: Settings,
    secret_id: str,
) -> str:
    envelope = store.get_secret(secret_id)
    if not envelope:
        raise KeyError(f"unknown one-time secret: {secret_id}")
    plaintext = OneTimeSecretCipher(settings).decrypt(envelope)
    store.mark_secret_read(secret_id)
    return plaintext
