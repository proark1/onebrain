"""JSON-backed in-process platform store."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.platform.base import (
    CUSTOMER_SERVICE_PURPOSES,
    PRIVATE_SPACE_KINDS,
    AccessDecision,
    Account,
    AppInstallation,
    AuditEvent,
    BrandTheme,
    ConsentRecord,
    CredentialMetadata,
    DataAccessEvent,
    LegalHold,
    Membership,
    Organization,
    ProcessorRegistration,
    ProviderRegistration,
    RetentionPolicy,
    RetentionRun,
    Space,
    default_brand_theme,
    normalize_unique,
    normalized_brand_theme,
    validate_account,
    validate_brand_theme,
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


def _brand_theme_to_dict(t: BrandTheme) -> dict:
    return {
        "id": t.id,
        "account_id": t.account_id,
        "app_id": t.app_id,
        "name": t.name,
        "primary_color": t.primary_color,
        "secondary_color": t.secondary_color,
        "accent_color": t.accent_color,
        "background_color": t.background_color,
        "surface_color": t.surface_color,
        "text_color": t.text_color,
        "muted_color": t.muted_color,
        "success_color": t.success_color,
        "warning_color": t.warning_color,
        "danger_color": t.danger_color,
        "logo_url": t.logo_url,
        "source": t.source,
        "status": t.status,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
    }


def _brand_theme_from_dict(d: dict) -> BrandTheme:
    return BrandTheme(
        id=d["id"],
        account_id=d["account_id"],
        app_id=d.get("app_id", ""),
        name=d.get("name", ""),
        primary_color=d.get("primary_color", ""),
        secondary_color=d.get("secondary_color", ""),
        accent_color=d.get("accent_color", ""),
        background_color=d.get("background_color", ""),
        surface_color=d.get("surface_color", ""),
        text_color=d.get("text_color", ""),
        muted_color=d.get("muted_color", ""),
        success_color=d.get("success_color", ""),
        warning_color=d.get("warning_color", ""),
        danger_color=d.get("danger_color", ""),
        logo_url=d.get("logo_url", ""),
        source=d.get("source", "operator"),
        status=d.get("status", "active"),
        created_at=d.get("created_at", ""),
        updated_at=d.get("updated_at", ""),
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
        self._brand_themes: Dict[str, BrandTheme] = {}
        self._audit: Dict[str, AuditEvent] = {}
        self._organizations: Dict[str, Organization] = {}
        self._memberships: Dict[str, Membership] = {}
        self._consent_records: Dict[str, ConsentRecord] = {}
        self._retention_policies: Dict[str, RetentionPolicy] = {}
        self._legal_holds: Dict[str, LegalHold] = {}
        self._retention_runs: Dict[str, RetentionRun] = {}
        self._data_access_events: Dict[str, DataAccessEvent] = {}
        self._processors: Dict[str, ProcessorRegistration] = {}
        self._providers: Dict[str, ProviderRegistration] = {}
        self._credential_metadata: Dict[str, CredentialMetadata] = {}
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
            self._brand_themes = {d["id"]: _brand_theme_from_dict(d) for d in data.get("brand_themes", [])}
            self._audit = {d["id"]: _audit_from_dict(d) for d in data.get("audit", [])}
            self._organizations = {d["id"]: Organization(**d) for d in data.get("organizations", [])}
            self._memberships = {d["id"]: Membership(**d) for d in data.get("memberships", [])}
            self._consent_records = {d["id"]: ConsentRecord(**d) for d in data.get("consent_records", [])}
            self._retention_policies = {d["id"]: RetentionPolicy(**d) for d in data.get("retention_policies", [])}
            self._legal_holds = {d["id"]: LegalHold(**d) for d in data.get("legal_holds", [])}
            self._retention_runs = {d["id"]: RetentionRun(**d) for d in data.get("retention_runs", [])}
            self._data_access_events = {d["id"]: DataAccessEvent(**d) for d in data.get("data_access_events", [])}
            self._processors = {d["id"]: ProcessorRegistration(**d) for d in data.get("processors", [])}
            self._providers = {d["id"]: ProviderRegistration(**d) for d in data.get("providers", [])}
            self._credential_metadata = {d["id"]: CredentialMetadata(**d) for d in data.get("credential_metadata", [])}
        except Exception:
            self._accounts, self._spaces, self._installations, self._brand_themes, self._audit = {}, {}, {}, {}, {}
            self._organizations, self._memberships, self._consent_records, self._retention_policies = {}, {}, {}, {}
            self._legal_holds, self._retention_runs = {}, {}
            self._data_access_events, self._processors, self._providers, self._credential_metadata = {}, {}, {}, {}

    def _save(self) -> None:
        if not self._persist_path:
            return
        os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
        with open(self._persist_path, "w", encoding="utf-8") as fh:
            json.dump({
                "accounts": [_account_to_dict(a) for a in self._accounts.values()],
                "spaces": [_space_to_dict(s) for s in self._spaces.values()],
                "installations": [_installation_to_dict(i) for i in self._installations.values()],
                "brand_themes": [_brand_theme_to_dict(t) for t in self._brand_themes.values()],
                "audit": [_audit_to_dict(e) for e in self._audit.values()],
                "organizations": [asdict(v) for v in self._organizations.values()],
                "memberships": [asdict(v) for v in self._memberships.values()],
                "consent_records": [asdict(v) for v in self._consent_records.values()],
                "retention_policies": [asdict(v) for v in self._retention_policies.values()],
                "legal_holds": [asdict(v) for v in self._legal_holds.values()],
                "retention_runs": [asdict(v) for v in self._retention_runs.values()],
                "data_access_events": [asdict(v) for v in self._data_access_events.values()],
                "processors": [asdict(v) for v in self._processors.values()],
                "providers": [asdict(v) for v in self._providers.values()],
                "credential_metadata": [asdict(v) for v in self._credential_metadata.values()],
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

    def upsert_brand_theme(self, theme: BrandTheme) -> BrandTheme:
        theme = normalized_brand_theme(theme)
        validate_brand_theme(theme)
        with self._lock:
            if theme.account_id not in self._accounts:
                raise ValueError(f"unknown account: {theme.account_id}")
            if theme.app_id:
                installed = any(
                    i.account_id == theme.account_id and i.app_id == theme.app_id
                    for i in self._installations.values()
                )
                if not installed:
                    raise ValueError(f"app is not installed in this account: {theme.app_id}")
            for existing_id, existing in list(self._brand_themes.items()):
                if existing.account_id == theme.account_id and existing.app_id == theme.app_id and existing_id != theme.id:
                    del self._brand_themes[existing_id]
            self._brand_themes[theme.id] = theme
            self._save()
            return theme

    def get_brand_theme(self, account_id: str, app_id: str = "") -> Optional[BrandTheme]:
        app_id = (app_id or "").strip()
        matches = [
            theme for theme in self._brand_themes.values()
            if theme.account_id == account_id and theme.app_id == app_id and theme.status == "active"
        ]
        return sorted(matches, key=lambda theme: theme.updated_at or theme.created_at or theme.id)[-1] if matches else None

    def list_brand_themes(self, account_id: str) -> List[BrandTheme]:
        return sorted(
            (theme for theme in self._brand_themes.values() if theme.account_id == account_id),
            key=lambda theme: (theme.app_id, theme.name.lower(), theme.id),
        )

    def resolve_brand_theme(self, account_id: str, app_id: str = "") -> BrandTheme:
        app_id = (app_id or "").strip()
        if app_id:
            app_theme = self.get_brand_theme(account_id, app_id)
            if app_theme:
                return app_theme
        account_theme = self.get_brand_theme(account_id)
        return account_theme or default_brand_theme(account_id, app_id)

    def record_audit(self, event: AuditEvent) -> AuditEvent:
        with self._lock:
            if event.id in self._audit:
                raise ValueError(f"audit event already exists: {event.id}")
            self._audit[event.id] = event
            self._save()
            return event

    def list_audit(self, account_id: str) -> List[AuditEvent]:
        return [e for e in self._audit.values() if e.account_id == account_id]

    def _require_account(self, account_id: str) -> None:
        if account_id and account_id not in self._accounts:
            raise ValueError(f"unknown account: {account_id}")

    def _require_space(self, account_id: str, space_id: str) -> None:
        if not space_id:
            return
        space = self._spaces.get(space_id)
        if not space or space.account_id != account_id:
            raise ValueError(f"space is not in this account: {space_id}")

    def upsert_organization(self, organization: Organization) -> Organization:
        self._require_account(organization.account_id)
        with self._lock:
            self._organizations[organization.id] = organization
            self._save()
            return organization

    def list_organizations(self, account_id: str) -> List[Organization]:
        return sorted((v for v in self._organizations.values() if v.account_id == account_id), key=lambda v: v.name.lower())

    def upsert_membership(self, membership: Membership) -> Membership:
        self._require_account(membership.account_id)
        self._require_space(membership.account_id, membership.space_id)
        with self._lock:
            self._memberships[membership.id] = membership
            self._save()
            return membership

    def list_memberships(self, account_id: str) -> List[Membership]:
        return sorted((v for v in self._memberships.values() if v.account_id == account_id), key=lambda v: (v.user_id, v.id))

    def upsert_consent_record(self, record: ConsentRecord) -> ConsentRecord:
        self._require_account(record.account_id)
        self._require_space(record.account_id, record.space_id)
        with self._lock:
            self._consent_records[record.id] = record
            self._save()
            return record

    def list_consent_records(self, account_id: str, space_id: str = "") -> List[ConsentRecord]:
        rows = [v for v in self._consent_records.values() if v.account_id == account_id]
        if space_id:
            rows = [v for v in rows if v.space_id == space_id]
        return sorted(rows, key=lambda v: (v.created_at, v.id))

    def upsert_retention_policy(self, policy: RetentionPolicy) -> RetentionPolicy:
        self._require_account(policy.account_id)
        self._require_space(policy.account_id, policy.space_id)
        if policy.duration_days < 0:
            raise ValueError("retention duration must be non-negative")
        with self._lock:
            self._retention_policies[policy.id] = policy
            self._save()
            return policy

    def list_retention_policies(self, account_id: str, space_id: str = "") -> List[RetentionPolicy]:
        rows = [v for v in self._retention_policies.values() if v.account_id == account_id]
        if space_id:
            rows = [v for v in rows if v.space_id == space_id]
        return sorted(rows, key=lambda v: (v.domain, v.record_type, v.id))

    def create_legal_hold(self, hold: LegalHold) -> LegalHold:
        self._require_account(hold.account_id)
        self._require_space(hold.account_id, hold.space_id)
        with self._lock:
            self._legal_holds[hold.id] = hold
            self._save()
            return hold

    def list_legal_holds(self, account_id: str, space_id: str = "", include_released: bool = False) -> List[LegalHold]:
        rows = [v for v in self._legal_holds.values() if v.account_id == account_id]
        if space_id:
            rows = [v for v in rows if v.space_id == space_id]
        if not include_released:
            rows = [v for v in rows if not v.released_at]
        return sorted(rows, key=lambda v: (v.created_at, v.id))

    def release_legal_hold(self, account_id: str, hold_id: str, released_at: str = "") -> Optional[LegalHold]:
        with self._lock:
            hold = self._legal_holds.get(hold_id)
            if not hold or hold.account_id != account_id:
                return None
            if hold.released_at:
                return hold
            released = replace(hold, released_at=released_at or datetime.now(timezone.utc).isoformat())
            self._legal_holds[hold_id] = released
            self._save()
            return released

    def record_retention_run(self, run: RetentionRun) -> RetentionRun:
        self._require_account(run.account_id)
        with self._lock:
            self._retention_runs[run.id] = run
            self._save()
            return run

    def list_retention_runs(self, account_id: str, space_id: str = "") -> List[RetentionRun]:
        rows = [v for v in self._retention_runs.values() if v.account_id == account_id]
        if space_id:
            rows = [v for v in rows if v.space_id == space_id]
        return sorted(rows, key=lambda v: (v.created_at, v.id))

    def record_data_access(self, event: DataAccessEvent) -> DataAccessEvent:
        self._require_account(event.account_id)
        self._require_space(event.account_id, event.space_id)
        with self._lock:
            if event.id in self._data_access_events:
                raise ValueError(f"data access event already exists: {event.id}")
            self._data_access_events[event.id] = event
            self._save()
            return event

    def list_data_access_events(self, account_id: str, space_id: str = "") -> List[DataAccessEvent]:
        rows = [v for v in self._data_access_events.values() if v.account_id == account_id]
        if space_id:
            rows = [v for v in rows if v.space_id == space_id]
        return sorted(rows, key=lambda v: (v.created_at, v.id))

    def upsert_processor(self, processor: ProcessorRegistration) -> ProcessorRegistration:
        self._require_account(processor.account_id)
        with self._lock:
            self._processors[processor.id] = processor
            self._save()
            return processor

    def list_processors(self, account_id: str = "") -> List[ProcessorRegistration]:
        rows = list(self._processors.values())
        if account_id:
            rows = [v for v in rows if v.account_id in {"", account_id}]
        return sorted(rows, key=lambda v: (v.name.lower(), v.id))

    def upsert_provider(self, provider: ProviderRegistration) -> ProviderRegistration:
        self._require_account(provider.account_id)
        with self._lock:
            self._providers[provider.id] = provider
            self._save()
            return provider

    def list_providers(self, account_id: str = "") -> List[ProviderRegistration]:
        rows = list(self._providers.values())
        if account_id:
            rows = [v for v in rows if v.account_id in {"", account_id}]
        return sorted(rows, key=lambda v: (v.name.lower(), v.id))

    def upsert_credential_metadata(self, credential: CredentialMetadata) -> CredentialMetadata:
        self._require_account(credential.account_id)
        with self._lock:
            self._credential_metadata[credential.id] = credential
            self._save()
            return credential

    def list_credential_metadata(self, account_id: str) -> List[CredentialMetadata]:
        return sorted((v for v in self._credential_metadata.values() if v.account_id == account_id), key=lambda v: (v.provider, v.id))

    def delete_governance_by_scope(self, account_id: str, space_id: str = "") -> dict[str, int]:
        with self._lock:
            counts = {
                "organizations": 0,
                "memberships": 0,
                "consent_records": 0,
                "retention_policies": 0,
                "data_access_events": 0,
                "credential_metadata": 0,
            }
            if not space_id:
                counts["organizations"] = self._delete_matching(self._organizations, lambda v: v.account_id == account_id)
                counts["credential_metadata"] = self._delete_matching(
                    self._credential_metadata, lambda v: v.account_id == account_id,
                )
            counts["memberships"] = self._delete_matching(
                self._memberships,
                lambda v: v.account_id == account_id and (not space_id or v.space_id == space_id),
            )
            counts["consent_records"] = self._delete_matching(
                self._consent_records,
                lambda v: v.account_id == account_id and (not space_id or v.space_id == space_id),
            )
            counts["retention_policies"] = self._delete_matching(
                self._retention_policies,
                lambda v: v.account_id == account_id and (not space_id or v.space_id == space_id),
            )
            counts["data_access_events"] = self._delete_matching(
                self._data_access_events,
                lambda v: v.account_id == account_id and (not space_id or v.space_id == space_id),
            )
            self._save()
            return counts

    def _delete_matching(self, values: dict, predicate) -> int:
        keys = [key for key, value in values.items() if predicate(value)]
        for key in keys:
            del values[key]
        return len(keys)
