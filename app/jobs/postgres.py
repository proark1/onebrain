"""Postgres-backed durable job store."""

from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

from app.db.schema import validate_postgres_schema
from app.jobs.base import (
    JobFailureSummary,
    JobSummary,
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
        "attempts, max_attempts, run_after, locked_by, locked_at, created_at, updated_at, completed_at"
    )

    def __init__(self, dsn: str):
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._validate_schema()

    def _conn(self):
        return self._psycopg.connect(self._dsn)

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
    ) -> Job:
        job_id = f"job_{uuid4().hex}"
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO jobs "
                f"(id, type, status, tenant_id, account_id, space_id, requested_by, payload, max_attempts) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {self._JOB_COLS}",
                (
                    job_id, type, STATUS_QUEUED, tenant_id, account_id, space_id, requested_by,
                    json.dumps(payload or {}), max_attempts,
                ),
            )
            row = cur.fetchone()
            if file is not None:
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

    def get(self, job_id: str) -> Job | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._JOB_COLS} FROM jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
        return self._job(row) if row else None

    def get_file(self, job_id: str) -> JobFile | None:
        with self._conn() as conn, conn.cursor() as cur:
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

    def claim(self, worker_id: str, limit: int = 1) -> list[Job]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                WITH claimed AS (
                    SELECT id
                    FROM jobs
                    WHERE status IN ('queued', 'retrying')
                      AND run_after <= now()
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE jobs j
                SET status = 'running',
                    attempts = attempts + 1,
                    locked_by = %s,
                    locked_at = now(),
                    updated_at = now()
                FROM claimed
                WHERE j.id = claimed.id
                RETURNING {self._JOB_COLS}
                """,
                (max(1, limit), worker_id),
            )
            rows = cur.fetchall()
            conn.commit()
        return [self._job(row) for row in rows]

    def mark_succeeded(self, job_id: str, result: dict) -> Job:
        return self._mark_terminal(job_id, STATUS_SUCCEEDED, result=result)

    def mark_failed(self, job_id: str, error: str) -> Job:
        return self._mark_terminal(job_id, STATUS_FAILED, error=error)

    def mark_retry(self, job_id: str, error: str, run_after: datetime) -> Job:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE jobs
                SET status = %s,
                    error = %s,
                    run_after = %s,
                    locked_by = '',
                    locked_at = NULL,
                    updated_at = now()
                WHERE id = %s
                RETURNING {self._JOB_COLS}
                """,
                (STATUS_RETRYING, error[:2000], run_after, job_id),
            )
            row = cur.fetchone()
            conn.commit()
        if not row:
            raise KeyError(f"unknown job: {job_id}")
        return self._job(row)

    def summary(self, recent_failures_limit: int = 10) -> JobSummary:
        with self._conn() as conn, conn.cursor() as cur:
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

    def _mark_terminal(self, job_id: str, status: str, result: dict | None = None, error: str = "") -> Job:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE jobs
                SET status = %s,
                    result = %s,
                    error = %s,
                    locked_by = '',
                    locked_at = NULL,
                    updated_at = now(),
                    completed_at = now()
                WHERE id = %s
                RETURNING {self._JOB_COLS}
                """,
                (status, json.dumps(result) if result is not None else None, error[:2000], job_id),
            )
            row = cur.fetchone()
            conn.commit()
        if not row:
            raise KeyError(f"unknown job: {job_id}")
        return self._job(row)

    def _job(self, row) -> Job:
        return Job(
            id=row[0], type=row[1], status=row[2], tenant_id=row[3], account_id=row[4],
            space_id=row[5], requested_by=row[6], payload=_json(row[7]) or {},
            result=_json(row[8]), error=row[9] or "", attempts=int(row[10] or 0),
            max_attempts=int(row[11] or 0), run_after=_iso(row[12]), locked_by=row[13] or "",
            locked_at=_iso(row[14]), created_at=_iso(row[15]), updated_at=_iso(row[16]),
            completed_at=_iso(row[17]),
        )
