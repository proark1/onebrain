"""Postgres-backed platform store."""

from __future__ import annotations

import json
from typing import List, Optional

from app.platform.base import (
    CUSTOMER_SERVICE_PURPOSES,
    PRIVATE_SPACE_KINDS,
    AccessDecision,
    Account,
    AppInstallation,
    AuditEvent,
    Space,
    normalize_unique,
    validate_account,
    validate_installation,
    validate_space,
)


def _join(values: tuple[str, ...]) -> str:
    return ",".join(normalize_unique(values))


def _split(value: str) -> tuple[str, ...]:
    return normalize_unique((value or "").split(","))


class PostgresPlatformStore:
    def __init__(self, dsn: str):
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._init_schema()

    def _conn(self):
        return self._psycopg.connect(self._dsn)

    def _init_schema(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS platform_accounts (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS platform_spaces (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS platform_spaces_account_idx ON platform_spaces (account_id)")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS platform_app_installations (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
                    app_id TEXT NOT NULL,
                    enabled_space_ids TEXT NOT NULL,
                    allowed_purposes TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS platform_app_installations_account_idx "
                "ON platform_app_installations (account_id, app_id)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS platform_audit_events (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL DEFAULT '',
                    actor_type TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    space_id TEXT NOT NULL DEFAULT '',
                    app_id TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL DEFAULT '',
                    decision TEXT NOT NULL DEFAULT '',
                    meta TEXT NOT NULL DEFAULT '{}',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS platform_audit_account_idx ON platform_audit_events (account_id)")
            conn.commit()

    def _account_row(self, r) -> Account:
        return Account(id=r[0], kind=r[1], name=r[2], owner_user_id=r[3], status=r[4],
                       created_at=r[5].isoformat() if r[5] else "")

    def _space_row(self, r) -> Space:
        return Space(id=r[0], account_id=r[1], kind=r[2], name=r[3], status=r[4],
                     created_at=r[5].isoformat() if r[5] else "")

    def _installation_row(self, r) -> AppInstallation:
        return AppInstallation(id=r[0], account_id=r[1], app_id=r[2], enabled_space_ids=_split(r[3]),
                               allowed_purposes=_split(r[4]), display_name=r[5], status=r[6],
                               created_at=r[7].isoformat() if r[7] else "")

    def _audit_row(self, r) -> AuditEvent:
        try:
            meta = json.loads(r[11] or "{}")
        except Exception:
            meta = {}
        return AuditEvent(id=r[0], account_id=r[1], actor_id=r[2], actor_type=r[3], action=r[4],
                          target_type=r[5], target_id=r[6], space_id=r[7], app_id=r[8],
                          purpose=r[9], decision=r[10], meta=meta,
                          created_at=r[12].isoformat() if r[12] else "")

    def create_account(self, account: Account) -> Account:
        validate_account(account)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO platform_accounts (id, kind, name, owner_user_id, status) VALUES (%s, %s, %s, %s, %s)",
                (account.id, account.kind, account.name, account.owner_user_id, account.status),
            )
            conn.commit()
        return account

    def get_account(self, account_id: str) -> Optional[Account]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, kind, name, owner_user_id, status, created_at FROM platform_accounts WHERE id = %s",
                        (account_id,))
            row = cur.fetchone()
        return self._account_row(row) if row else None

    def list_accounts(self) -> List[Account]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, kind, name, owner_user_id, status, created_at FROM platform_accounts ORDER BY name")
            rows = cur.fetchall()
        return [self._account_row(r) for r in rows]

    def create_space(self, space: Space) -> Space:
        validate_space(space)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO platform_spaces (id, account_id, kind, name, status) VALUES (%s, %s, %s, %s, %s)",
                (space.id, space.account_id, space.kind, space.name, space.status),
            )
            conn.commit()
        return space

    def get_space(self, space_id: str) -> Optional[Space]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, account_id, kind, name, status, created_at FROM platform_spaces WHERE id = %s",
                        (space_id,))
            row = cur.fetchone()
        return self._space_row(row) if row else None

    def list_spaces(self, account_id: str) -> List[Space]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, account_id, kind, name, status, created_at FROM platform_spaces "
                "WHERE account_id = %s ORDER BY name",
                (account_id,),
            )
            rows = cur.fetchall()
        return [self._space_row(r) for r in rows]

    def install_app(self, installation: AppInstallation) -> AppInstallation:
        validate_installation(installation)
        for space_id in installation.enabled_space_ids:
            space = self.get_space(space_id)
            if not space or space.account_id != installation.account_id:
                raise ValueError(f"space is not in this account: {space_id}")
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO platform_app_installations "
                "(id, account_id, app_id, enabled_space_ids, allowed_purposes, display_name, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (installation.id, installation.account_id, installation.app_id, _join(installation.enabled_space_ids),
                 _join(installation.allowed_purposes), installation.display_name, installation.status),
            )
            conn.commit()
        return installation

    def get_app_installation(self, installation_id: str) -> Optional[AppInstallation]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, account_id, app_id, enabled_space_ids, allowed_purposes, display_name, status, created_at "
                "FROM platform_app_installations WHERE id = %s",
                (installation_id,),
            )
            row = cur.fetchone()
        return self._installation_row(row) if row else None

    def list_app_installations(self, account_id: str) -> List[AppInstallation]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, account_id, app_id, enabled_space_ids, allowed_purposes, display_name, status, created_at "
                "FROM platform_app_installations WHERE account_id = %s ORDER BY app_id",
                (account_id,),
            )
            rows = cur.fetchall()
        return [self._installation_row(r) for r in rows]

    def check_app_access(self, account_id: str, app_id: str, space_id: str, purpose: str) -> AccessDecision:
        space = self.get_space(space_id)
        if not space or space.account_id != account_id or space.status != "active":
            return AccessDecision(False, "space_not_found")
        if purpose in CUSTOMER_SERVICE_PURPOSES and space.kind in PRIVATE_SPACE_KINDS:
            return AccessDecision(False, "customer_service_cannot_use_private_space")
        for installation in self.list_app_installations(account_id):
            if installation.app_id != app_id or installation.status != "active":
                continue
            if space_id in installation.enabled_space_ids and purpose in installation.allowed_purposes:
                return AccessDecision(True)
        return AccessDecision(False, "purpose_or_space_not_enabled")

    def record_audit(self, event: AuditEvent) -> AuditEvent:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO platform_audit_events "
                "(id, account_id, actor_id, actor_type, action, target_type, target_id, space_id, app_id, purpose, decision, meta) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (event.id, event.account_id, event.actor_id, event.actor_type, event.action, event.target_type,
                 event.target_id, event.space_id, event.app_id, event.purpose, event.decision, json.dumps(event.meta)),
            )
            conn.commit()
        return event

    def list_audit(self, account_id: str) -> List[AuditEvent]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, account_id, actor_id, actor_type, action, target_type, target_id, "
                "space_id, app_id, purpose, decision, meta, created_at FROM platform_audit_events "
                "WHERE account_id = %s ORDER BY created_at",
                (account_id,),
            )
            rows = cur.fetchall()
        return [self._audit_row(r) for r in rows]
