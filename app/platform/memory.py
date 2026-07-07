"""JSON-backed in-process platform store."""

from __future__ import annotations

import json
import os
import threading
from typing import Dict, List, Optional

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


def _account_to_dict(a: Account) -> dict:
    return {"id": a.id, "kind": a.kind, "name": a.name, "owner_user_id": a.owner_user_id,
            "status": a.status, "created_at": a.created_at}


def _account_from_dict(d: dict) -> Account:
    return Account(id=d["id"], kind=d["kind"], name=d["name"], owner_user_id=d.get("owner_user_id", ""),
                   status=d.get("status", "active"), created_at=d.get("created_at", ""))


def _space_to_dict(s: Space) -> dict:
    return {"id": s.id, "account_id": s.account_id, "kind": s.kind, "name": s.name,
            "status": s.status, "created_at": s.created_at}


def _space_from_dict(d: dict) -> Space:
    return Space(id=d["id"], account_id=d["account_id"], kind=d["kind"], name=d["name"],
                 status=d.get("status", "active"), created_at=d.get("created_at", ""))


def _installation_to_dict(i: AppInstallation) -> dict:
    return {"id": i.id, "account_id": i.account_id, "app_id": i.app_id,
            "enabled_space_ids": list(i.enabled_space_ids), "allowed_purposes": list(i.allowed_purposes),
            "display_name": i.display_name, "status": i.status, "created_at": i.created_at}


def _installation_from_dict(d: dict) -> AppInstallation:
    return AppInstallation(
        id=d["id"], account_id=d["account_id"], app_id=d["app_id"],
        enabled_space_ids=normalize_unique(d.get("enabled_space_ids", [])),
        allowed_purposes=normalize_unique(d.get("allowed_purposes", [])),
        display_name=d.get("display_name", ""), status=d.get("status", "active"),
        created_at=d.get("created_at", ""),
    )


def _audit_to_dict(e: AuditEvent) -> dict:
    return {"id": e.id, "account_id": e.account_id, "actor_id": e.actor_id, "actor_type": e.actor_type,
            "action": e.action, "target_type": e.target_type, "target_id": e.target_id,
            "space_id": e.space_id, "app_id": e.app_id, "purpose": e.purpose,
            "decision": e.decision, "meta": e.meta, "created_at": e.created_at}


def _audit_from_dict(d: dict) -> AuditEvent:
    return AuditEvent(
        id=d["id"], account_id=d["account_id"], actor_id=d.get("actor_id", ""), actor_type=d.get("actor_type", ""),
        action=d.get("action", ""), target_type=d.get("target_type", ""), target_id=d.get("target_id", ""),
        space_id=d.get("space_id", ""), app_id=d.get("app_id", ""), purpose=d.get("purpose", ""),
        decision=d.get("decision", ""), meta=d.get("meta", {}), created_at=d.get("created_at", ""),
    )


class MemoryPlatformStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._accounts: Dict[str, Account] = {}
        self._spaces: Dict[str, Space] = {}
        self._installations: Dict[str, AppInstallation] = {}
        self._audit: Dict[str, AuditEvent] = {}
        self._lock = threading.RLock()
        self._persist_path = persist_path
        self._load()

    def _load(self) -> None:
        if not (self._persist_path and os.path.exists(self._persist_path)):
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._accounts = {d["id"]: _account_from_dict(d) for d in data.get("accounts", [])}
            self._spaces = {d["id"]: _space_from_dict(d) for d in data.get("spaces", [])}
            self._installations = {d["id"]: _installation_from_dict(d) for d in data.get("installations", [])}
            self._audit = {d["id"]: _audit_from_dict(d) for d in data.get("audit", [])}
        except Exception:
            self._accounts, self._spaces, self._installations, self._audit = {}, {}, {}, {}

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        with open(self._persist_path, "w", encoding="utf-8") as fh:
            json.dump({
                "accounts": [_account_to_dict(a) for a in self._accounts.values()],
                "spaces": [_space_to_dict(s) for s in self._spaces.values()],
                "installations": [_installation_to_dict(i) for i in self._installations.values()],
                "audit": [_audit_to_dict(e) for e in self._audit.values()],
            }, fh)

    def create_account(self, account: Account) -> Account:
        validate_account(account)
        with self._lock:
            if account.id in self._accounts:
                raise ValueError(f"account already exists: {account.id}")
            self._accounts[account.id] = account
            self._save()
            return account

    def get_account(self, account_id: str) -> Optional[Account]:
        return self._accounts.get(account_id)

    def list_accounts(self) -> List[Account]:
        return sorted(self._accounts.values(), key=lambda a: a.name.lower())

    def create_space(self, space: Space) -> Space:
        validate_space(space)
        with self._lock:
            if space.id in self._spaces:
                raise ValueError(f"space already exists: {space.id}")
            if space.account_id not in self._accounts:
                raise ValueError(f"unknown account: {space.account_id}")
            self._spaces[space.id] = space
            self._save()
            return space

    def get_space(self, space_id: str) -> Optional[Space]:
        return self._spaces.get(space_id)

    def list_spaces(self, account_id: str) -> List[Space]:
        return sorted((s for s in self._spaces.values() if s.account_id == account_id), key=lambda s: s.name.lower())

    def install_app(self, installation: AppInstallation) -> AppInstallation:
        validate_installation(installation)
        installation = AppInstallation(
            id=installation.id,
            account_id=installation.account_id,
            app_id=installation.app_id,
            enabled_space_ids=normalize_unique(installation.enabled_space_ids),
            allowed_purposes=normalize_unique(installation.allowed_purposes),
            display_name=installation.display_name,
            status=installation.status,
            created_at=installation.created_at,
        )
        with self._lock:
            if installation.id in self._installations:
                raise ValueError(f"app installation already exists: {installation.id}")
            if installation.account_id not in self._accounts:
                raise ValueError(f"unknown account: {installation.account_id}")
            for space_id in installation.enabled_space_ids:
                space = self._spaces.get(space_id)
                if not space or space.account_id != installation.account_id:
                    raise ValueError(f"space is not in this account: {space_id}")
            self._installations[installation.id] = installation
            self._save()
            return installation

    def get_app_installation(self, installation_id: str) -> Optional[AppInstallation]:
        return self._installations.get(installation_id)

    def list_app_installations(self, account_id: str) -> List[AppInstallation]:
        return sorted((i for i in self._installations.values() if i.account_id == account_id),
                      key=lambda i: i.app_id)

    def check_app_access(self, account_id: str, app_id: str, space_id: str, purpose: str) -> AccessDecision:
        space = self._spaces.get(space_id)
        if not space or space.account_id != account_id or space.status != "active":
            return AccessDecision(False, "space_not_found")
        if purpose in CUSTOMER_SERVICE_PURPOSES and space.kind in PRIVATE_SPACE_KINDS:
            return AccessDecision(False, "customer_service_cannot_use_private_space")
        matches = [
            i for i in self._installations.values()
            if i.account_id == account_id and i.app_id == app_id and i.status == "active"
        ]
        if not matches:
            return AccessDecision(False, "app_not_installed")
        for installation in matches:
            if space_id in installation.enabled_space_ids and purpose in installation.allowed_purposes:
                return AccessDecision(True)
        return AccessDecision(False, "purpose_or_space_not_enabled")

    def record_audit(self, event: AuditEvent) -> AuditEvent:
        with self._lock:
            if event.id in self._audit:
                raise ValueError(f"audit event already exists: {event.id}")
            self._audit[event.id] = event
            self._save()
            return event

    def list_audit(self, account_id: str) -> List[AuditEvent]:
        return [e for e in self._audit.values() if e.account_id == account_id]
