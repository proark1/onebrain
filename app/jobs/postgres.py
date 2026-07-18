"""Postgres-backed durable job store."""

from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

from app.db.rls import set_rls_scope
from app.db.schema import validate_postgres_schema
from app.jobs.base import (
    JobFailureSummary,
    JobScopeDeleteResult,
    JobSummary,
    JobLeaseLostError,
    LEASE_EXPIRED_ERROR,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RETRYING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    Job,
    JobFile,
    JobFileInput,
)


KNOWN_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_RETRYING, STATUS_SUCCEEDED, STATUS_FAILED)


class JobWorkerAccessError(RuntimeError):
    """Raised when a worker-only queue operation runs without its DSN."""


def _iso(value) -> str:
    return value.isoformat() if value else ""


def _json(value) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


class PostgresJobStore:
    _JOB_COLS = (
        "id, type, status, tenant_id, account_id, space_id, requested_by, payload, result, error, "
        "attempts, max_attempts, run_after, locked_by, locked_at, lease_token, lease_expires_at, "
        "created_at, updated_at, completed_at"
    )
    _JOB_COLS_J = (
        "j.id, j.type, j.status, j.tenant_id, j.account_id, j.space_id, j.requested_by, "
        "j.payload, j.result, j.error, j.attempts, j.max_attempts, j.run_after, "
        "j.locked_by, j.locked_at, j.lease_token, j.lease_expires_at, j.created_at, j.updated_at, "
        "j.completed_at"
    )

    # Keep positional shape identical to _JOB_COLS so _job() remains one
    # mapper, but replace worker-only payload/lease fields with inert values.
    # The request role therefore never SELECTs queue inputs or worker fencing
    # capabilities (and lacks column privileges for both).
    _APP_JOB_COLS = (
        "id, type, status, tenant_id, account_id, space_id, requested_by, "
        "'{}'::jsonb AS payload, result, error, attempts, 0 AS max_attempts, "
        "NULL::timestamptz AS run_after, ''::text AS locked_by, "
        "NULL::timestamptz AS locked_at, ''::text AS lease_token, "
        "NULL::timestamptz AS lease_expires_at, created_at, updated_at, completed_at"
    )

    def __init__(
        self,
        dsn: str,
        *,
        worker_dsn: str = "",
        operator_dsn: str = "",
    ):
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._worker_dsn = worker_dsn.strip()
        self._operator_dsn = operator_dsn.strip()
        self._validate_schema()

    def _conn(self):
        return self._psycopg.connect(self._dsn)

    def _worker_conn(self):
        if not self._worker_dsn:
            raise JobWorkerAccessError(
                "Worker-only job operation requires ONEBRAIN_WORKER_DATABASE_URL."
            )
        return self._psycopg.connect(self._worker_dsn)

    def _operator_conn(self):
        if not self._operator_dsn:
            raise JobWorkerAccessError(
                "Cross-tenant job summary requires the privileged operator database DSN."
            )
        return self._psycopg.connect(self._operator_dsn)

    def _validate_schema(self) -> None:
        with self._conn() as conn:
            validate_postgres_schema(conn, ("jobs", "job_files"))

    def enqueue(
        self,
        *,
        type: str,
        tenant_id: str,
        account_id: str = "",
        space_id: str = "",
        requested_by: str = "",
        payload: dict | None = None,
        file: JobFileInput | None = None,
        max_attempts: int = 3,
        idempotency_key: str = "",
    ) -> Job:
        job_id = f"job_{uuid4().hex}"
        dedupe = (idempotency_key or "").strip()
        if len(dedupe) > 200:
            raise ValueError("Job idempotency key is too long.")
        with self._conn() as conn, conn.cursor() as cur:
            set_rls_scope(
                conn,
                tenant_id=tenant_id,
                account_id=account_id,
                space_id=space_id,
            )
            cur.execute(
                f"INSERT INTO jobs "
                f"(id, type, status, tenant_id, account_id, space_id, requested_by, payload, "
                f"max_attempts, idempotency_key) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                f"ON CONFLICT (tenant_id, account_id, space_id, type, idempotency_key) "
                f"WHERE idempotency_key <> '' DO NOTHING RETURNING {self._APP_JOB_COLS}",
                (
                    job_id, type, STATUS_QUEUED, tenant_id, account_id, space_id, requested_by,
                    json.dumps(payload or {}), max_attempts, dedupe,
                ),
            )
            row = cur.fetchone()
            created = row is not None
            if not row:
                cur.execute(
                    f"SELECT {self._APP_JOB_COLS} FROM jobs WHERE tenant_id=%s "
                    "AND account_id=%s AND space_id=%s AND type=%s AND idempotency_key=%s",
                    (tenant_id, account_id, space_id, type, dedupe),
                )
                row = cur.fetchone()
                if not row:
                    raise RuntimeError("Idempotent job enqueue could not recover its existing row.")
            if file is not None and created:
                cur.execute(
                    "INSERT INTO job_files (id, job_id, filename, content_type, size_bytes, data) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        f"file_{uuid4().hex}", job_id, file.filename, file.content_type,
                        file.size_bytes, file.data,
                    ),
                )
            conn.commit()
        return self._job(row)

    def get(
        self,
        job_id: str,
        *,
        tenant_id: str = "",
        account_id: str = "",
        space_id: str = "",
    ) -> Job | None:
        """Read a client-safe job status through the request role's RLS scope."""
        with self._conn() as conn, conn.cursor() as cur:
            set_rls_scope(
                conn,
                tenant_id=tenant_id,
                account_id=account_id,
                space_id=space_id,
            )
            cur.execute(f"SELECT {self._APP_JOB_COLS} FROM jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
        return self._job(row) if row else None

    def get_file(self, job_id: str) -> JobFile | None:
        """Return an uploaded file for a claimed job using the worker login.

        No HTTP route calls this method.  API containers do not receive a worker
        DSN, so they cannot turn it into a cross-tenant file-read capability.
        """
        with self._worker_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, job_id, filename, content_type, size_bytes, data, created_at "
                "FROM job_files WHERE job_id = %s ORDER BY created_at LIMIT 1",
                (job_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return JobFile(
            id=row[0], job_id=row[1], filename=row[2], content_type=row[3],
            size_bytes=row[4], data=bytes(row[5]), created_at=_iso(row[6]),
        )

    def claim(self, worker_id: str, limit: int = 1, lease_seconds: int = 60) -> list[Job]:
        lease_seconds = max(1, int(lease_seconds))
        lease_token = f"lease_{uuid4().hex}"
        with self._worker_conn() as conn, conn.cursor() as cur:
            # Finish a lease that already used its final attempt before exposing
            # anything for claim. SKIP LOCKED keeps concurrent workers from
            # racing the terminalization or reclaim of the same job.
            cur.execute(
                """
                WITH exhausted AS (
                    SELECT id
                    FROM jobs
                    WHERE status = %s
                      AND (lease_expires_at IS NULL OR lease_expires_at <= now())
                      AND attempts >= max_attempts
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE jobs j
                SET status = %s,
                    result = NULL,
                    error = %s,
                    locked_by = '',
                    locked_at = NULL,
                    lease_token = '',
                    lease_expires_at = NULL,
                    updated_at = now(),
                    completed_at = now()
                FROM exhausted
                WHERE j.id = exhausted.id
                RETURNING j.id
                """,
                (STATUS_RUNNING, STATUS_FAILED, LEASE_EXPIRED_ERROR),
            )
            exhausted_ids = [row[0] for row in cur.fetchall()]
            if exhausted_ids:
                # Keep this as a second statement in the same transaction. A
                # worker DELETE policy may require the parent job to be
                # terminal, and a later statement observes the update above.
                cur.execute(
                    "DELETE FROM job_files WHERE job_id = ANY(%s)",
                    (exhausted_ids,),
                )
            cur.execute(
                f"""
                WITH claimed AS (
                    SELECT id
                    FROM jobs
                    WHERE (
                        status IN (%s, %s)
                        AND run_after <= now()
                    ) OR (
                        status = %s
                        AND (lease_expires_at IS NULL OR lease_expires_at <= now())
                        AND attempts < max_attempts
                    )
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE jobs j
                SET status = 'running',
                    attempts = attempts + 1,
                    locked_by = %s,
                    locked_at = now(),
                    lease_token = %s || ':' || j.id,
                    lease_expires_at = now() + (%s * INTERVAL '1 second'),
                    updated_at = now()
                FROM claimed
                WHERE j.id = claimed.id
                RETURNING {self._JOB_COLS_J}
                """,
                (
                    STATUS_QUEUED,
                    STATUS_RETRYING,
                    STATUS_RUNNING,
                    max(1, limit),
                    worker_id,
                    lease_token,
                    lease_seconds,
                ),
            )
            rows = cur.fetchall()
            conn.commit()
        return [self._job(row) for row in rows]

    def renew_lease(self, job_id: str, lease_token: str, lease_seconds: int) -> Job:
        lease_seconds = max(1, int(lease_seconds))
        with self._worker_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE jobs
                SET lease_expires_at = now() + (%s * INTERVAL '1 second'),
                    updated_at = now()
                WHERE id = %s
                  AND status = %s
                  AND lease_token = %s
                  AND lease_expires_at > now()
                RETURNING {self._JOB_COLS}
                """,
                (lease_seconds, job_id, STATUS_RUNNING, lease_token),
            )
            row = cur.fetchone()
            conn.commit()
        if not row:
            self._raise_missing_or_lost_lease(job_id)
        return self._job(row)

    def mark_succeeded(self, job_id: str, result: dict, *, lease_token: str) -> Job:
        return self._mark_terminal(job_id, STATUS_SUCCEEDED, lease_token=lease_token, result=result)

    def mark_failed(self, job_id: str, error: str, *, lease_token: str) -> Job:
        return self._mark_terminal(job_id, STATUS_FAILED, lease_token=lease_token, error=error)

    def mark_retry(self, job_id: str, error: str, run_after: datetime, *, lease_token: str) -> Job:
        with self._worker_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE jobs
                SET status = %s,
                    error = %s,
                    run_after = %s,
                    locked_by = '',
                    locked_at = NULL,
                    lease_token = '',
                    lease_expires_at = NULL,
                    updated_at = now()
                WHERE id = %s
                  AND status = %s
                  AND lease_token = %s
                  AND lease_expires_at > now()
                RETURNING {self._JOB_COLS}
                """,
                (STATUS_RETRYING, error[:2000], run_after, job_id, STATUS_RUNNING, lease_token),
            )
            row = cur.fetchone()
            conn.commit()
        if not row:
            self._raise_missing_or_lost_lease(job_id)
        return self._job(row)

    def delete_scope(
        self,
        tenant_id: str,
        *,
        account_id: str = "",
        space_id: str = "",
    ) -> JobScopeDeleteResult:
        """Delete a privacy scope's jobs and transient file bytes atomically."""

        tenant_id = (tenant_id or "").strip()
        account_id = (account_id or "").strip()
        space_id = (space_id or "").strip()
        clauses = ["j.tenant_id = %s"]
        params: list = [tenant_id]
        if space_id:
            clauses.extend(("j.account_id = %s", "j.space_id = %s"))
            params.extend((account_id, space_id))
        elif account_id:
            # Include legacy jobs created before account scope was stamped,
            # matching the existing document privacy-erasure contract.
            clauses.append("j.account_id = ANY(%s)")
            params.append(["", account_id])
        where = " AND ".join(clauses)

        with self._conn() as conn, conn.cursor() as cur:
            # Account-wide erasure needs to see both the selected account and
            # legacy account_id='' rows. Keep the database GUC tenant-scoped and
            # let the explicit predicate above narrow the account rows.
            set_rls_scope(
                conn,
                tenant_id=tenant_id,
                account_id=account_id if space_id else "",
                space_id=space_id,
            )
            cur.execute(
                f"""
                WITH deleted AS (
                    DELETE FROM job_files f
                    USING jobs j
                    WHERE f.job_id = j.id AND {where}
                    RETURNING 1
                )
                SELECT count(*) FROM deleted
                """,
                params,
            )
            file_row = cur.fetchone()
            files_deleted = int(file_row[0] or 0) if file_row else 0
            job_where = where.replace("j.", "")
            cur.execute(f"DELETE FROM jobs WHERE {job_where}", params)
            jobs_deleted = int(cur.rowcount or 0)
            conn.commit()
        return JobScopeDeleteResult(jobs=jobs_deleted, files=files_deleted)

    def summary(self, recent_failures_limit: int = 10) -> JobSummary:
        # Only the separate operator surface needs cross-tenant aggregate data;
        # it receives the operator DSN, never the worker credential.
        with self._operator_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM jobs")
            total = int(cur.fetchone()[0])
            cur.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")
            by_status = {status: 0 for status in KNOWN_STATUSES}
            by_status.update({str(row[0]): int(row[1]) for row in cur.fetchall()})
            cur.execute("SELECT type, COUNT(*) FROM jobs GROUP BY type")
            by_type = {str(row[0]): int(row[1]) for row in cur.fetchall()}
            cur.execute(
                """
                SELECT id, type, tenant_id, account_id, space_id, attempts, max_attempts,
                       error, created_at, updated_at, completed_at
                FROM jobs
                WHERE status = %s
                ORDER BY completed_at DESC NULLS LAST, updated_at DESC, created_at DESC, id DESC
                LIMIT %s
                """,
                (STATUS_FAILED, max(0, recent_failures_limit)),
            )
            failures = [
                JobFailureSummary(
                    id=row[0],
                    type=row[1],
                    tenant_id=row[2],
                    account_id=row[3],
                    space_id=row[4],
                    attempts=int(row[5] or 0),
                    max_attempts=int(row[6] or 0),
                    error=(row[7] or "")[:500],
                    created_at=_iso(row[8]),
                    updated_at=_iso(row[9]),
                    completed_at=_iso(row[10]),
                )
                for row in cur.fetchall()
            ]
        return JobSummary(total=total, by_status=by_status, by_type=by_type, recent_failures=failures)

    def _mark_terminal(
        self,
        job_id: str,
        status: str,
        *,
        lease_token: str,
        result: dict | None = None,
        error: str = "",
    ) -> Job:
        with self._worker_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE jobs
                SET status = %s,
                    result = %s,
                    error = %s,
                    locked_by = '',
                    locked_at = NULL,
                    lease_token = '',
                    lease_expires_at = NULL,
                    updated_at = now(),
                    completed_at = now()
                WHERE id = %s
                  AND status = %s
                  AND lease_token = %s
                  AND lease_expires_at > now()
                RETURNING {self._JOB_COLS}
                """,
                (
                    status,
                    json.dumps(result) if result is not None else None,
                    error[:2000],
                    job_id,
                    STATUS_RUNNING,
                    lease_token,
                ),
            )
            row = cur.fetchone()
            if row:
                # Bytes are retry material only. The fenced transition and
                # deletion share this transaction so terminal status can never
                # commit while its uploaded payload remains behind.
                cur.execute("DELETE FROM job_files WHERE job_id = %s", (job_id,))
            conn.commit()
        if not row:
            self._raise_missing_or_lost_lease(job_id)
        return self._job(row)

    def _raise_missing_or_lost_lease(self, job_id: str) -> None:
        if self._get_for_worker(job_id) is None:
            raise KeyError(f"unknown job: {job_id}")
        raise JobLeaseLostError(f"job lease is no longer active: {job_id}")

    def _get_for_worker(self, job_id: str) -> Job | None:
        with self._worker_conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._JOB_COLS} FROM jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
        return self._job(row) if row else None

    def _job(self, row) -> Job:
        return Job(
            id=row[0], type=row[1], status=row[2], tenant_id=row[3], account_id=row[4],
            space_id=row[5], requested_by=row[6], payload=_json(row[7]) or {},
            result=_json(row[8]), error=row[9] or "", attempts=int(row[10] or 0),
            max_attempts=int(row[11] or 0), run_after=_iso(row[12]), locked_by=row[13] or "",
            locked_at=_iso(row[14]), lease_token=row[15] or "", lease_expires_at=_iso(row[16]),
            created_at=_iso(row[17]), updated_at=_iso(row[18]), completed_at=_iso(row[19]),
        )
