"""Postgres-backed fleet store (Mission Control deployment)."""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from app.db.schema import validate_postgres_schema
from app.fleet.base import FleetAlert, FleetKey, Heartbeat


def _iso(value) -> str:
    return value.isoformat() if value else ""


class PostgresFleetStore:
    def __init__(self, dsn: str):
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._validate_schema()

    def _conn(self):
        return self._psycopg.connect(self._dsn)

    def _validate_schema(self) -> None:
        with self._conn() as conn:
            validate_postgres_schema(conn, ("fleet_keys", "fleet_heartbeats", "fleet_alerts"))

    # --- keys ---
    def _key_row(self, r) -> FleetKey:
        return FleetKey(id=r[0], key_hash=r[1], deployment_id=r[2], label=r[3] or "",
                        status=r[4], created_at=_iso(r[5]), last_used_at=_iso(r[6]))

    def create_key(self, key: FleetKey) -> FleetKey:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fleet_keys (id, key_hash, deployment_id, label, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                (key.id, key.key_hash, key.deployment_id, key.label, key.status),
            )
            conn.commit()
        return key

    def get_key(self, key_id: str) -> Optional[FleetKey]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, key_hash, deployment_id, label, status, created_at, last_used_at "
                        "FROM fleet_keys WHERE id = %s", (key_id,))
            row = cur.fetchone()
        return self._key_row(row) if row else None

    def list_keys(self, deployment_id: str = "") -> List[FleetKey]:
        clause = "WHERE deployment_id = %s" if deployment_id else ""
        params = (deployment_id,) if deployment_id else ()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, key_hash, deployment_id, label, status, created_at, last_used_at "
                        f"FROM fleet_keys {clause} ORDER BY deployment_id, created_at, id", params)
            return [self._key_row(r) for r in cur.fetchall()]

    def revoke_key(self, key_id: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE fleet_keys SET status = 'revoked' WHERE id = %s AND status = 'active'", (key_id,))
            changed = cur.rowcount
            conn.commit()
        return changed > 0

    def touch_key(self, key_id: str, now_iso: str) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE fleet_keys SET last_used_at = %s::timestamptz WHERE id = %s", (now_iso, key_id))
            conn.commit()

    # --- heartbeats ---
    def _hb_row(self, r) -> Heartbeat:
        return Heartbeat(id=r[0], deployment_id=r[1], contract_version=r[2], reported_at=_iso(r[3]),
                         received_at=_iso(r[4]), healthy=bool(r[5]), version=r[6] or "",
                         migration_revision=r[7] or "", payload=r[8] or {})

    _HB_COLS = ("id, deployment_id, contract_version, reported_at, received_at, healthy, "
                "version, migration_revision, payload")

    def record_heartbeat(self, heartbeat: Heartbeat) -> Heartbeat:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO fleet_heartbeats ({self._HB_COLS}) "
                "VALUES (%s, %s, %s, %s::timestamptz, now(), %s, %s, %s, %s)",
                (heartbeat.id, heartbeat.deployment_id, heartbeat.contract_version, heartbeat.reported_at,
                 heartbeat.healthy, heartbeat.version, heartbeat.migration_revision, json.dumps(heartbeat.payload)),
            )
            conn.commit()
        return heartbeat

    def latest_heartbeat(self, deployment_id: str) -> Optional[Heartbeat]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._HB_COLS} FROM fleet_heartbeats WHERE deployment_id = %s "
                        "ORDER BY received_at DESC LIMIT 1", (deployment_id,))
            row = cur.fetchone()
        return self._hb_row(row) if row else None

    def latest_heartbeats(self) -> Dict[str, Heartbeat]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT DISTINCT ON (deployment_id) {self._HB_COLS} FROM fleet_heartbeats "
                        "ORDER BY deployment_id, received_at DESC")
            return {r[1]: self._hb_row(r) for r in cur.fetchall()}

    # --- alerts ---
    def _alert_row(self, r) -> FleetAlert:
        return FleetAlert(id=r[0], deployment_id=r[1], kind=r[2], detail=r[3] or "",
                          status=r[4], created_at=_iso(r[5]), resolved_at=_iso(r[6]))

    def open_alert(self, alert: FleetAlert) -> FleetAlert:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fleet_alerts (id, deployment_id, kind, detail, status) VALUES (%s, %s, %s, %s, 'open')",
                (alert.id, alert.deployment_id, alert.kind, alert.detail),
            )
            conn.commit()
        return alert

    def resolve_open_alerts(self, deployment_id: str, kind: str, resolved_at: str) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_alerts SET status = 'resolved', resolved_at = %s::timestamptz "
                "WHERE deployment_id = %s AND kind = %s AND status = 'open'",
                (resolved_at, deployment_id, kind),
            )
            changed = cur.rowcount
            conn.commit()
        return int(changed)

    def list_open_alerts(self, deployment_id: str = "") -> List[FleetAlert]:
        clause = "AND deployment_id = %s" if deployment_id else ""
        params = (deployment_id,) if deployment_id else ()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, deployment_id, kind, detail, status, created_at, resolved_at "
                        f"FROM fleet_alerts WHERE status = 'open' {clause} "
                        "ORDER BY deployment_id, created_at, id", params)
            return [self._alert_row(r) for r in cur.fetchall()]

    def has_open_alert(self, deployment_id: str, kind: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM fleet_alerts WHERE deployment_id = %s AND kind = %s "
                        "AND status = 'open' LIMIT 1", (deployment_id, kind))
            return cur.fetchone() is not None
