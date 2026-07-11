"""In-process fleet store with optional JSON persistence (local/dev/test)."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict
from typing import Dict, List, Optional

from app.fleet.base import FleetAlert, FleetKey, Heartbeat


class MemoryFleetStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._keys: Dict[str, FleetKey] = {}
        self._latest: Dict[str, Heartbeat] = {}
        self._history: Dict[str, List[Heartbeat]] = {}
        self._history_cap = 2000  # per deployment, in-memory bound
        self._alerts: Dict[str, FleetAlert] = {}
        self._lock = threading.RLock()
        self._persist_path = persist_path
        self._load()

    def _load(self) -> None:
        if not (self._persist_path and os.path.exists(self._persist_path)):
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._keys = {d["id"]: FleetKey(**d) for d in data.get("keys", [])}
            self._latest = {d["deployment_id"]: Heartbeat(**d) for d in data.get("latest", [])}
            self._alerts = {d["id"]: FleetAlert(**d) for d in data.get("alerts", [])}
        except Exception:
            self._keys, self._latest, self._alerts = {}, {}, {}

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        with open(self._persist_path, "w", encoding="utf-8") as fh:
            json.dump({
                "keys": [asdict(v) for v in self._keys.values()],
                "latest": [asdict(v) for v in self._latest.values()],
                "alerts": [asdict(v) for v in self._alerts.values()],
            }, fh)

    # --- keys ---
    def create_key(self, key: FleetKey) -> FleetKey:
        with self._lock:
            self._keys[key.id] = key
            self._save()
            return key

    def get_key(self, key_id: str) -> Optional[FleetKey]:
        return self._keys.get(key_id)

    def list_keys(self, deployment_id: str = "") -> List[FleetKey]:
        rows = [k for k in self._keys.values() if not deployment_id or k.deployment_id == deployment_id]
        return sorted(rows, key=lambda k: (k.deployment_id, k.created_at, k.id))

    def revoke_key(self, key_id: str) -> bool:
        with self._lock:
            key = self._keys.get(key_id)
            if not key or key.status == "revoked":
                return False
            self._keys[key_id] = FleetKey(**{**asdict(key), "status": "revoked"})
            self._save()
            return True

    def touch_key(self, key_id: str, now_iso: str) -> None:
        with self._lock:
            key = self._keys.get(key_id)
            if key:
                self._keys[key_id] = FleetKey(**{**asdict(key), "last_used_at": now_iso})
                self._save()

    # --- heartbeats ---
    def record_heartbeat(self, heartbeat: Heartbeat) -> Heartbeat:
        with self._lock:
            self._latest[heartbeat.deployment_id] = heartbeat
            hist = self._history.setdefault(heartbeat.deployment_id, [])
            hist.append(heartbeat)
            if len(hist) > self._history_cap:
                del hist[: len(hist) - self._history_cap]
            self._save()
            return heartbeat

    def latest_heartbeat(self, deployment_id: str) -> Optional[Heartbeat]:
        return self._latest.get(deployment_id)

    def latest_heartbeats(self) -> Dict[str, Heartbeat]:
        return dict(self._latest)

    def list_heartbeats(self, deployment_id: str, since_iso: str = "", limit: int = 500) -> List[Heartbeat]:
        # Note: _history is in-memory only (not JSON-persisted) — the memory store
        # is dev/test; production history lives in Postgres fleet_heartbeats.
        rows = [hb for hb in self._history.get(deployment_id, []) if not since_iso or hb.received_at >= since_iso]
        return sorted(rows, key=lambda hb: hb.received_at, reverse=True)[:max(1, min(limit, 5000))]

    def prune_heartbeats(self, before_iso: str) -> int:
        with self._lock:
            removed = 0
            for deployment_id, rows in list(self._history.items()):
                kept = [hb for hb in rows if hb.received_at >= before_iso]
                removed += len(rows) - len(kept)
                self._history[deployment_id] = kept
            if removed:
                self._save()
            return removed

    # --- alerts ---
    def open_alert(self, alert: FleetAlert) -> FleetAlert:
        with self._lock:
            self._alerts[alert.id] = alert
            self._save()
            return alert

    def resolve_open_alerts(self, deployment_id: str, kind: str, resolved_at: str) -> int:
        with self._lock:
            count = 0
            for aid, alert in list(self._alerts.items()):
                if alert.deployment_id == deployment_id and alert.kind == kind and alert.status == "open":
                    self._alerts[aid] = FleetAlert(**{**asdict(alert), "status": "resolved", "resolved_at": resolved_at})
                    count += 1
            if count:
                self._save()
            return count

    def list_open_alerts(self, deployment_id: str = "") -> List[FleetAlert]:
        rows = [
            a for a in self._alerts.values()
            if a.status == "open" and (not deployment_id or a.deployment_id == deployment_id)
        ]
        return sorted(rows, key=lambda a: (a.deployment_id, a.created_at, a.id))

    def has_open_alert(self, deployment_id: str, kind: str) -> bool:
        return any(
            a.deployment_id == deployment_id and a.kind == kind and a.status == "open"
            for a in self._alerts.values()
        )
