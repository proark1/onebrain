"""Core platform records and store contract.

This is the first slice of the unified-platform plan: the small, explicit model
that lets OneBrain know which account owns data, which space it belongs to, and
which installed app may use it for which purpose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

from app.assistant.contracts import ASSISTANT_PURPOSES


ACCOUNT_KINDS = frozenset({"person", "organization", "family", "project"})
SPACE_KINDS = frozenset({"personal", "business", "customer_service", "shared", "family", "project"})
APP_IDS = frozenset({"onebrain_core", "assistant", "communication", "admin_console", "workers"})
# Assistant purposes come from the assistant contract so the platform registry
# cannot drift behind it (drift here rejects valid assistant writes as 422s).
PURPOSES = ASSISTANT_PURPOSES | frozenset({
    "customer_service_answer",
    "customer_service_inbox",
    "knowledge_management",
    "admin_management",
    "gdpr_export",
    "gdpr_delete",
    "analytics",
    "billing",
})
CUSTOMER_SERVICE_PURPOSES = frozenset({"customer_service_answer", "customer_service_inbox"})
PRIVATE_SPACE_KINDS = frozenset({"personal", "family"})
BRAND_COLOR_FIELDS = (
    "primary_color",
    "secondary_color",
    "accent_color",
    "background_color",
    "surface_color",
    "text_color",
    "muted_color",
    "success_color",
    "warning_color",
    "danger_color",
)
DEFAULT_BRAND_THEME = {
    "name": "Assad Dar",
    "primary_color": "#16191e",
    "secondary_color": "#3e5573",
    "accent_color": "#a66e2f",
    "background_color": "#f4f2ee",
    "surface_color": "#ffffff",
    "text_color": "#101828",
    "muted_color": "#5f6671",
    "success_color": "#1f7a4d",
    "warning_color": "#b98a4e",
    "danger_color": "#b4453e",
    "logo_url": "",
}
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass(frozen=True)
class Account:
    id: str
    kind: str
    name: str
    owner_user_id: str = ""
    status: str = "active"
    created_at: str = ""


@dataclass(frozen=True)
class Space:
    id: str
    account_id: str
    kind: str
    name: str
    status: str = "active"
    created_at: str = ""


@dataclass(frozen=True)
class AppInstallation:
    id: str
    account_id: str
    app_id: str
    enabled_space_ids: tuple[str, ...]
    allowed_purposes: tuple[str, ...]
    display_name: str = ""
    status: str = "active"
    created_at: str = ""


@dataclass(frozen=True)
class BrandTheme:
    id: str
    account_id: str
    app_id: str = ""
    name: str = ""
    primary_color: str = ""
    secondary_color: str = ""
    accent_color: str = ""
    background_color: str = ""
    surface_color: str = ""
    text_color: str = ""
    muted_color: str = ""
    success_color: str = ""
    warning_color: str = ""
    danger_color: str = ""
    logo_url: str = ""
    source: str = "operator"
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str = "allowed"


@dataclass(frozen=True)
class AuditEvent:
    id: str
    account_id: str
    actor_id: str
    actor_type: str
    action: str
    target_type: str
    target_id: str
    space_id: str = ""
    app_id: str = ""
    purpose: str = ""
    decision: str = ""
    meta: Dict = field(default_factory=dict)
    created_at: str = ""


@dataclass(frozen=True)
class Organization:
    id: str
    account_id: str
    name: str
    status: str = "active"
    created_at: str = ""


@dataclass(frozen=True)
class Membership:
    id: str
    account_id: str
    user_id: str
    role_id: str
    space_id: str = ""
    organization_id: str = ""
    status: str = "active"
    created_at: str = ""


@dataclass(frozen=True)
class ConsentRecord:
    id: str
    account_id: str
    subject_ref: str
    purpose: str
    status: str
    space_id: str = ""
    source: str = ""
    captured_by: str = ""
    withdrawn_at: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class RetentionPolicy:
    id: str
    account_id: str
    domain: str
    record_type: str
    action: str
    duration_days: int
    legal_basis: str
    space_id: str = ""
    status: str = "active"
    created_at: str = ""


@dataclass(frozen=True)
class DataAccessEvent:
    id: str
    account_id: str
    actor_id: str
    actor_type: str
    action: str
    target_type: str
    target_id: str
    space_id: str = ""
    app_id: str = ""
    purpose: str = ""
    decision: str = ""
    meta: Dict = field(default_factory=dict)
    created_at: str = ""


@dataclass(frozen=True)
class ProcessorRegistration:
    id: str
    name: str
    category: str
    region: str
    dpa_status: str
    transfer_mechanism: str = ""
    account_id: str = ""
    status: str = "active"
    meta: Dict = field(default_factory=dict)
    created_at: str = ""


@dataclass(frozen=True)
class ProviderRegistration:
    id: str
    name: str
    category: str
    region: str
    dpia_status: str
    transfer_mechanism: str = ""
    account_id: str = ""
    status: str = "active"
    meta: Dict = field(default_factory=dict)
    created_at: str = ""


@dataclass(frozen=True)
class CredentialMetadata:
    id: str
    account_id: str
    provider: str
    app_id: str
    secret_ref: str
    status: str = "active"
    rotated_at: str = ""
    last_verified_at: str = ""
    meta: Dict = field(default_factory=dict)
    created_at: str = ""


class PlatformStore(Protocol):
    def create_account(self, account: Account) -> Account: ...

    def get_account(self, account_id: str) -> Optional[Account]: ...

    def list_accounts(self) -> List[Account]: ...

    def create_space(self, space: Space) -> Space: ...

    def get_space(self, space_id: str) -> Optional[Space]: ...

    def list_spaces(self, account_id: str) -> List[Space]: ...

    def install_app(self, installation: AppInstallation) -> AppInstallation: ...

    def get_app_installation(self, installation_id: str) -> Optional[AppInstallation]: ...

    def list_app_installations(self, account_id: str) -> List[AppInstallation]: ...

    def check_app_access(self, account_id: str, app_id: str, space_id: str, purpose: str) -> AccessDecision: ...

    def upsert_brand_theme(self, theme: BrandTheme) -> BrandTheme: ...

    def get_brand_theme(self, account_id: str, app_id: str = "") -> Optional[BrandTheme]: ...

    def list_brand_themes(self, account_id: str) -> List[BrandTheme]: ...

    def resolve_brand_theme(self, account_id: str, app_id: str = "") -> BrandTheme: ...

    def record_audit(self, event: AuditEvent) -> AuditEvent: ...

    def list_audit(self, account_id: str) -> List[AuditEvent]: ...

    def upsert_organization(self, organization: Organization) -> Organization: ...

    def list_organizations(self, account_id: str) -> List[Organization]: ...

    def upsert_membership(self, membership: Membership) -> Membership: ...

    def list_memberships(self, account_id: str) -> List[Membership]: ...

    def upsert_consent_record(self, record: ConsentRecord) -> ConsentRecord: ...

    def list_consent_records(self, account_id: str, space_id: str = "") -> List[ConsentRecord]: ...

    def upsert_retention_policy(self, policy: RetentionPolicy) -> RetentionPolicy: ...

    def list_retention_policies(self, account_id: str, space_id: str = "") -> List[RetentionPolicy]: ...

    def record_data_access(self, event: DataAccessEvent) -> DataAccessEvent: ...

    def list_data_access_events(self, account_id: str, space_id: str = "") -> List[DataAccessEvent]: ...

    def upsert_processor(self, processor: ProcessorRegistration) -> ProcessorRegistration: ...

    def list_processors(self, account_id: str = "") -> List[ProcessorRegistration]: ...

    def upsert_provider(self, provider: ProviderRegistration) -> ProviderRegistration: ...

    def list_providers(self, account_id: str = "") -> List[ProviderRegistration]: ...

    def upsert_credential_metadata(self, credential: CredentialMetadata) -> CredentialMetadata: ...

    def list_credential_metadata(self, account_id: str) -> List[CredentialMetadata]: ...

    def delete_governance_by_scope(self, account_id: str, space_id: str = "") -> Dict[str, int]: ...


def normalize_unique(values) -> tuple[str, ...]:
    """Trim, dedupe, and preserve order for ids/purposes stored as tuples."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        item = str(value).strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def normalize_hex_color(value: str) -> str:
    color = (value or "").strip()
    if len(color) == 4 and color.startswith("#"):
        color = "#" + "".join(ch * 2 for ch in color[1:])
    if not _HEX_RE.match(color):
        raise ValueError(f"Invalid hex color: {value}")
    return color.lower()


def default_brand_theme(account_id: str, app_id: str = "") -> BrandTheme:
    return BrandTheme(
        id=f"brand_{account_id}_{app_id or 'account'}",
        account_id=account_id,
        app_id=app_id,
        source="default",
        **DEFAULT_BRAND_THEME,
    )


def normalized_brand_theme(theme: BrandTheme) -> BrandTheme:
    values = {field: normalize_hex_color(getattr(theme, field)) for field in BRAND_COLOR_FIELDS}
    app_id = (theme.app_id or "").strip()
    if app_id and app_id not in APP_IDS:
        raise ValueError(f"Unknown app id: {app_id}")
    theme_id = theme.id.strip() or f"brand_{theme.account_id}_{app_id or 'account'}"
    return BrandTheme(
        id=theme_id,
        account_id=theme.account_id.strip(),
        app_id=app_id,
        name=(theme.name or DEFAULT_BRAND_THEME["name"]).strip(),
        logo_url=(theme.logo_url or "").strip(),
        source=(theme.source or "operator").strip(),
        status=(theme.status or "active").strip(),
        created_at=theme.created_at,
        updated_at=theme.updated_at,
        **values,
    )


def validate_account(account: Account) -> None:
    if account.kind not in ACCOUNT_KINDS:
        raise ValueError(f"Unknown account kind: {account.kind}")
    if not account.id.strip() or not account.name.strip():
        raise ValueError("Account id and name are required.")


def validate_space(space: Space) -> None:
    if space.kind not in SPACE_KINDS:
        raise ValueError(f"Unknown space kind: {space.kind}")
    if not space.id.strip() or not space.account_id.strip() or not space.name.strip():
        raise ValueError("Space id, account id and name are required.")


def validate_installation(installation: AppInstallation) -> None:
    if installation.app_id not in APP_IDS:
        raise ValueError(f"Unknown app id: {installation.app_id}")
    invalid = [p for p in installation.allowed_purposes if p not in PURPOSES]
    if invalid:
        raise ValueError(f"Unknown purposes: {invalid}")
    if not installation.id.strip() or not installation.account_id.strip():
        raise ValueError("Installation id and account id are required.")


def validate_brand_theme(theme: BrandTheme) -> None:
    if not theme.id.strip() or not theme.account_id.strip():
        raise ValueError("Brand theme id and account id are required.")
    if theme.app_id and theme.app_id not in APP_IDS:
        raise ValueError(f"Unknown app id: {theme.app_id}")
    for field in BRAND_COLOR_FIELDS:
        normalize_hex_color(getattr(theme, field))
