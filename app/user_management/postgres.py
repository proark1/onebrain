"""PostgreSQL stores for MC management jobs and customer encrypted receipts."""

from __future__ import annotations

from app.db.schema import validate_postgres_schema
from app.user_management.base import UserManagementJob, UserManagementReceipt


def _iso(value) -> str:
    return value.isoformat() if value else ""


class PostgresUserManagementJobStore:
    _COLS = (
        "id, deployment_id, action, status, idempotency_key, requested_by, "
        "sealed_payload, sealed_result_private_key, result_public_key, created_at, expires_at, "
        "leased_at, lease_expires_at, attempts, completed_at, result_sender_public_key, "
        "result_nonce, result_ciphertext, result_expires_at, result_consumed_at, error_code"
    )

    def __init__(self, dsn: str):
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        with self._conn() as conn:
            validate_postgres_schema(conn, ("fleet_user_management_jobs",))

    def _conn(self):
        return self._psycopg.connect(self._dsn)

    def _row(self, row) -> UserManagementJob:
        return UserManagementJob(
            id=row[0], deployment_id=row[1], action=row[2], status=row[3],
            idempotency_key=row[4], requested_by=row[5], sealed_payload=row[6],
            sealed_result_private_key=row[7], result_public_key=row[8],
            created_at=_iso(row[9]), expires_at=_iso(row[10]), leased_at=_iso(row[11]),
            lease_expires_at=_iso(row[12]), attempts=int(row[13]), completed_at=_iso(row[14]),
            result_sender_public_key=row[15] or "", result_nonce=row[16] or "",
            result_ciphertext=row[17] or "", result_expires_at=_iso(row[18]),
            result_consumed_at=_iso(row[19]), error_code=row[20] or "",
        )

    def create(self, job: UserManagementJob) -> UserManagementJob:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fleet_user_management_jobs "
                "(id, deployment_id, action, status, idempotency_key, requested_by, sealed_payload, "
                "sealed_result_private_key, result_public_key, created_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz)",
                (job.id, job.deployment_id, job.action, job.status, job.idempotency_key,
                 job.requested_by, job.sealed_payload, job.sealed_result_private_key,
                 job.result_public_key, job.created_at, job.expires_at),
            )
            conn.commit()
        return job

    def get(self, job_id: str) -> UserManagementJob | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._COLS} FROM fleet_user_management_jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
        return self._row(row) if row else None

    def list_for_deployment(self, deployment_id: str, limit: int = 100) -> list[UserManagementJob]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._COLS} FROM fleet_user_management_jobs WHERE deployment_id = %s "
                "ORDER BY created_at DESC, id DESC LIMIT %s",
                (deployment_id, max(1, min(limit, 500))),
            )
            return [self._row(row) for row in cur.fetchall()]

    def lease_next(self, deployment_id: str, *, now_iso: str, lease_expires_at: str) -> UserManagementJob | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_user_management_jobs SET status = 'expired', error_code = 'command_expired' "
                "WHERE deployment_id = %s AND status IN ('queued', 'leased') AND expires_at <= %s::timestamptz",
                (deployment_id, now_iso),
            )
            cur.execute(
                f"WITH candidate AS ("
                " SELECT id FROM fleet_user_management_jobs WHERE deployment_id = %s "
                " AND expires_at > %s::timestamptz "
                " AND (status = 'queued' OR (status = 'leased' AND lease_expires_at <= %s::timestamptz)) "
                " ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1"
                ") UPDATE fleet_user_management_jobs jobs SET status = 'leased', leased_at = %s::timestamptz, "
                "lease_expires_at = %s::timestamptz, attempts = jobs.attempts + 1 "
                "FROM candidate WHERE jobs.id = candidate.id RETURNING " + ", ".join(f"jobs.{c.strip()}" for c in self._COLS.split(",")),
                (deployment_id, now_iso, now_iso, now_iso, lease_expires_at),
            )
            row = cur.fetchone()
            conn.commit()
        return self._row(row) if row else None

    def complete(
        self, job_id: str, deployment_id: str, *, sender_public_key: str, nonce: str,
        ciphertext: str, completed_at: str, result_expires_at: str,
    ) -> UserManagementJob | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE fleet_user_management_jobs SET status = 'completed', completed_at = %s::timestamptz, "
                "lease_expires_at = NULL, result_sender_public_key = %s, result_nonce = %s, "
                "result_ciphertext = %s, result_expires_at = %s::timestamptz, error_code = '' "
                "WHERE id = %s AND deployment_id = %s AND status = 'leased' "
                f"RETURNING {self._COLS}",
                (completed_at, sender_public_key, nonce, ciphertext, result_expires_at, job_id, deployment_id),
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    f"SELECT {self._COLS} FROM fleet_user_management_jobs "
                    "WHERE id = %s AND deployment_id = %s AND status = 'completed'",
                    (job_id, deployment_id),
                )
                row = cur.fetchone()
            conn.commit()
        return self._row(row) if row else None

    def fail(self, job_id: str, deployment_id: str, *, error_code: str, completed_at: str) -> UserManagementJob | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE fleet_user_management_jobs SET status = 'failed', completed_at = %s::timestamptz, "
                "lease_expires_at = NULL, error_code = %s WHERE id = %s AND deployment_id = %s "
                "AND status IN ('queued', 'leased', 'completed') "
                f"RETURNING {self._COLS}",
                (completed_at, error_code, job_id, deployment_id),
            )
            row = cur.fetchone()
            conn.commit()
        return self._row(row) if row else None

    def consume_result(self, job_id: str, *, consumed_at: str) -> UserManagementJob | None:
        old_cols = ", ".join(f"selected.{c.strip()}" for c in self._COLS.split(","))
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"WITH selected AS MATERIALIZED ("
                f" SELECT {self._COLS} FROM fleet_user_management_jobs WHERE id = %s "
                " AND status = 'completed' AND result_consumed_at IS NULL "
                " AND result_expires_at > %s::timestamptz AND result_ciphertext <> '' FOR UPDATE"
                "), consumed AS ("
                " UPDATE fleet_user_management_jobs jobs SET result_consumed_at = %s::timestamptz, "
                "sealed_result_private_key = '', result_sender_public_key = '', result_nonce = '', result_ciphertext = '' "
                "FROM selected WHERE jobs.id = selected.id RETURNING jobs.id"
                f") SELECT {old_cols} FROM selected JOIN consumed ON consumed.id = selected.id",
                (job_id, consumed_at, consumed_at),
            )
            row = cur.fetchone()
            conn.commit()
        return self._row(row) if row else None

    def expire_and_purge(self, *, now_iso: str) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_user_management_jobs SET status = 'expired', error_code = 'command_expired' "
                "WHERE status IN ('queued', 'leased') AND expires_at <= %s::timestamptz",
                (now_iso,),
            )
            changed = cur.rowcount
            cur.execute(
                "UPDATE fleet_user_management_jobs SET sealed_result_private_key = '', "
                "result_sender_public_key = '', result_nonce = '', result_ciphertext = '' "
                "WHERE result_ciphertext <> '' AND result_expires_at <= %s::timestamptz",
                (now_iso,),
            )
            changed += cur.rowcount
            conn.commit()
        return int(changed)


class PostgresUserManagementReceiptStore:
    _COLS = "command_id, action, sender_public_key, nonce, ciphertext, created_at, expires_at"

    def __init__(self, dsn: str):
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        with self._conn() as conn:
            validate_postgres_schema(conn, ("user_management_receipts",))

    def _conn(self):
        return self._psycopg.connect(self._dsn)

    @staticmethod
    def _row(row) -> UserManagementReceipt:
        return UserManagementReceipt(
            command_id=row[0], action=row[1], sender_public_key=row[2], nonce=row[3],
            ciphertext=row[4], created_at=_iso(row[5]), expires_at=_iso(row[6]),
        )

    def get(self, command_id: str) -> UserManagementReceipt | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._COLS} FROM user_management_receipts WHERE command_id = %s", (command_id,))
            row = cur.fetchone()
        return self._row(row) if row else None

    def put(self, receipt: UserManagementReceipt) -> UserManagementReceipt:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_management_receipts "
                "(command_id, action, sender_public_key, nonce, ciphertext, created_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz) "
                "ON CONFLICT (command_id) DO NOTHING",
                (receipt.command_id, receipt.action, receipt.sender_public_key, receipt.nonce,
                 receipt.ciphertext, receipt.created_at, receipt.expires_at),
            )
            conn.commit()
        return self.get(receipt.command_id) or receipt

    def purge(self, *, now_iso: str) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM user_management_receipts WHERE expires_at <= %s::timestamptz", (now_iso,))
            changed = cur.rowcount
            conn.commit()
        return int(changed)
