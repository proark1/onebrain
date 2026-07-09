"""Postgres-backed platform store."""

from __future__ import annotations

import json
from typing import List, Optional

from app.db.rls import set_rls_scope
from app.db.schema import validate_postgres_schema
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
    Membership,
    Organization,
    ProcessorRegistration,
    ProviderRegistration,
    RetentionPolicy,
    Space,
    default_brand_theme,
    normalize_unique,
    normalized_brand_theme,
    validate_account,
    validate_brand_theme,
    validate_installation,
    validate_space,
)


def _join(values: tuple[str, ...]) -> str:
    return ",".join(normalize_unique(values))


def _split(value: str) -> tuple[str, ...]:
    return normalize_unique((value or "").split(","))


def _iso(value) -> str:
    return value.isoformat() if value else ""


def _json(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value or "{}")
    return dict(value)


class PostgresPlatformStore:
    def __init__(self, dsn: str):
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._validate_schema()

    def _conn(self, *, account_id: str = "", space_id: str = "", admin: bool = False):
        conn = self._psycopg.connect(self._dsn)
        if account_id or space_id or admin:
            set_rls_scope(conn, account_id=account_id, space_id=space_id, admin=admin)
        return conn

    def _validate_schema(self) -> None:
        with self._conn() as conn:
            validate_postgres_schema(
                conn,
                (
                    "platform_accounts",
                    "platform_spaces",
                    "platform_app_installations",
                    "platform_brand_themes",
                    "platform_audit_events",
                    "platform_organizations",
                    "platform_memberships",
                    "platform_consent_records",
                    "platform_retention_policies",
                    "platform_data_access_events",
                    "platform_processor_register",
                    "platform_provider_register",
                    "platform_credential_metadata",
                ),
            )

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

    def _brand_theme_row(self, r) -> BrandTheme:
        return BrandTheme(
            id=r[0],
            account_id=r[1],
            app_id=r[2],
            name=r[3],
            primary_color=r[4],
            secondary_color=r[5],
            accent_color=r[6],
            background_color=r[7],
            surface_color=r[8],
            text_color=r[9],
            muted_color=r[10],
            success_color=r[11],
            warning_color=r[12],
            danger_color=r[13],
            logo_url=r[14],
            source=r[15],
            status=r[16],
            created_at=r[17].isoformat() if r[17] else "",
            updated_at=r[18].isoformat() if r[18] else "",
        )

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
        with self._conn(admin=True) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO platform_accounts (id, kind, name, owner_user_id, status) VALUES (%s, %s, %s, %s, %s)",
                (account.id, account.kind, account.name, account.owner_user_id, account.status),
            )
            conn.commit()
        return account

    def get_account(self, account_id: str) -> Optional[Account]:
        with self._conn(account_id=account_id) as conn, conn.cursor() as cur:
            cur.execute("SELECT id, kind, name, owner_user_id, status, created_at FROM platform_accounts WHERE id = %s",
                        (account_id,))
            row = cur.fetchone()
        return self._account_row(row) if row else None

    def list_accounts(self) -> List[Account]:
        with self._conn(admin=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT id, kind, name, owner_user_id, status, created_at FROM platform_accounts ORDER BY name")
            rows = cur.fetchall()
        return [self._account_row(r) for r in rows]

    def create_space(self, space: Space) -> Space:
        validate_space(space)
        with self._conn(account_id=space.account_id) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO platform_spaces (id, account_id, kind, name, status) VALUES (%s, %s, %s, %s, %s)",
                (space.id, space.account_id, space.kind, space.name, space.status),
            )
            conn.commit()
        return space

    def get_space(self, space_id: str) -> Optional[Space]:
        with self._conn(admin=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT id, account_id, kind, name, status, created_at FROM platform_spaces WHERE id = %s",
                        (space_id,))
            row = cur.fetchone()
        return self._space_row(row) if row else None

    def list_spaces(self, account_id: str) -> List[Space]:
        with self._conn(account_id=account_id) as conn, conn.cursor() as cur:
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
        with self._conn(account_id=installation.account_id) as conn, conn.cursor() as cur:
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
        with self._conn(admin=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, account_id, app_id, enabled_space_ids, allowed_purposes, display_name, status, created_at "
                "FROM platform_app_installations WHERE id = %s",
                (installation_id,),
            )
            row = cur.fetchone()
        return self._installation_row(row) if row else None

    def list_app_installations(self, account_id: str) -> List[AppInstallation]:
        with self._conn(account_id=account_id) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, account_id, app_id, enabled_space_ids, allowed_purposes, display_name, status, created_at "
                "FROM platform_app_installations WHERE account_id = %s ORDER BY app_id",
                (account_id,),
            )
            rows = cur.fetchall()
        return [self._installation_row(r) for r in rows]

    def check_app_access(self, account_id: str, app_id: str, space_id: str, purpose: str) -> AccessDecision:
        with self._conn(account_id=account_id) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, account_id, kind, name, status, created_at "
                "FROM platform_spaces WHERE id = %s AND account_id = %s",
                (space_id, account_id),
            )
            row = cur.fetchone()
        space = self._space_row(row) if row else None
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

    def upsert_brand_theme(self, theme: BrandTheme) -> BrandTheme:
        theme = normalized_brand_theme(theme)
        validate_brand_theme(theme)
        if not self.get_account(theme.account_id):
            raise ValueError(f"unknown account: {theme.account_id}")
        if theme.app_id:
            installed = any(
                installation.app_id == theme.app_id
                for installation in self.list_app_installations(theme.account_id)
            )
            if not installed:
                raise ValueError(f"app is not installed in this account: {theme.app_id}")
        with self._conn(account_id=theme.account_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_brand_themes
                (id, account_id, app_id, name, primary_color, secondary_color, accent_color,
                 background_color, surface_color, text_color, muted_color, success_color,
                 warning_color, danger_color, logo_url, source, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_id, app_id) DO UPDATE SET
                    id = EXCLUDED.id,
                    name = EXCLUDED.name,
                    primary_color = EXCLUDED.primary_color,
                    secondary_color = EXCLUDED.secondary_color,
                    accent_color = EXCLUDED.accent_color,
                    background_color = EXCLUDED.background_color,
                    surface_color = EXCLUDED.surface_color,
                    text_color = EXCLUDED.text_color,
                    muted_color = EXCLUDED.muted_color,
                    success_color = EXCLUDED.success_color,
                    warning_color = EXCLUDED.warning_color,
                    danger_color = EXCLUDED.danger_color,
                    logo_url = EXCLUDED.logo_url,
                    source = EXCLUDED.source,
                    status = EXCLUDED.status,
                    updated_at = now()
                RETURNING id, account_id, app_id, name, primary_color, secondary_color,
                    accent_color, background_color, surface_color, text_color, muted_color,
                    success_color, warning_color, danger_color, logo_url, source, status,
                    created_at, updated_at
                """,
                (
                    theme.id,
                    theme.account_id,
                    theme.app_id,
                    theme.name,
                    theme.primary_color,
                    theme.secondary_color,
                    theme.accent_color,
                    theme.background_color,
                    theme.surface_color,
                    theme.text_color,
                    theme.muted_color,
                    theme.success_color,
                    theme.warning_color,
                    theme.danger_color,
                    theme.logo_url,
                    theme.source,
                    theme.status,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return self._brand_theme_row(row)

    def get_brand_theme(self, account_id: str, app_id: str = "") -> Optional[BrandTheme]:
        with self._conn(account_id=account_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, account_id, app_id, name, primary_color, secondary_color,
                    accent_color, background_color, surface_color, text_color, muted_color,
                    success_color, warning_color, danger_color, logo_url, source, status,
                    created_at, updated_at
                FROM platform_brand_themes
                WHERE account_id = %s AND app_id = %s AND status = 'active'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (account_id, (app_id or "").strip()),
            )
            row = cur.fetchone()
        return self._brand_theme_row(row) if row else None

    def list_brand_themes(self, account_id: str) -> List[BrandTheme]:
        with self._conn(account_id=account_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, account_id, app_id, name, primary_color, secondary_color,
                    accent_color, background_color, surface_color, text_color, muted_color,
                    success_color, warning_color, danger_color, logo_url, source, status,
                    created_at, updated_at
                FROM platform_brand_themes
                WHERE account_id = %s
                ORDER BY app_id, name, id
                """,
                (account_id,),
            )
            rows = cur.fetchall()
        return [self._brand_theme_row(r) for r in rows]

    def resolve_brand_theme(self, account_id: str, app_id: str = "") -> BrandTheme:
        app_id = (app_id or "").strip()
        if app_id:
            app_theme = self.get_brand_theme(account_id, app_id)
            if app_theme:
                return app_theme
        account_theme = self.get_brand_theme(account_id)
        return account_theme or default_brand_theme(account_id, app_id)

    def record_audit(self, event: AuditEvent) -> AuditEvent:
        with self._conn(account_id=event.account_id, space_id=event.space_id) as conn, conn.cursor() as cur:
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
        with self._conn(account_id=account_id) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, account_id, actor_id, actor_type, action, target_type, target_id, "
                "space_id, app_id, purpose, decision, meta, created_at FROM platform_audit_events "
                "WHERE account_id = %s ORDER BY created_at",
                (account_id,),
            )
            rows = cur.fetchall()
        return [self._audit_row(r) for r in rows]

    def upsert_organization(self, organization: Organization) -> Organization:
        if not self.get_account(organization.account_id):
            raise ValueError(f"unknown account: {organization.account_id}")
        with self._conn(account_id=organization.account_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_organizations (id, account_id, name, status)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, status = EXCLUDED.status
                RETURNING id, account_id, name, status, created_at
                """,
                (organization.id, organization.account_id, organization.name, organization.status),
            )
            row = cur.fetchone()
            conn.commit()
        return Organization(id=row[0], account_id=row[1], name=row[2], status=row[3], created_at=_iso(row[4]))

    def list_organizations(self, account_id: str) -> List[Organization]:
        with self._conn(account_id=account_id) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, account_id, name, status, created_at FROM platform_organizations WHERE account_id = %s ORDER BY name",
                (account_id,),
            )
            rows = cur.fetchall()
        return [Organization(id=r[0], account_id=r[1], name=r[2], status=r[3], created_at=_iso(r[4])) for r in rows]

    def upsert_membership(self, membership: Membership) -> Membership:
        if not self.get_account(membership.account_id):
            raise ValueError(f"unknown account: {membership.account_id}")
        if membership.space_id:
            space = self.get_space(membership.space_id)
            if not space or space.account_id != membership.account_id:
                raise ValueError(f"space is not in this account: {membership.space_id}")
        with self._conn(account_id=membership.account_id, space_id=membership.space_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_memberships
                (id, account_id, user_id, role_id, space_id, organization_id, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    user_id = EXCLUDED.user_id, role_id = EXCLUDED.role_id,
                    space_id = EXCLUDED.space_id, organization_id = EXCLUDED.organization_id,
                    status = EXCLUDED.status
                RETURNING id, account_id, user_id, role_id, space_id, organization_id, status, created_at
                """,
                (
                    membership.id, membership.account_id, membership.user_id, membership.role_id,
                    membership.space_id, membership.organization_id, membership.status,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return Membership(id=row[0], account_id=row[1], user_id=row[2], role_id=row[3], space_id=row[4],
                          organization_id=row[5], status=row[6], created_at=_iso(row[7]))

    def list_memberships(self, account_id: str) -> List[Membership]:
        with self._conn(account_id=account_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, account_id, user_id, role_id, space_id, organization_id, status, created_at
                FROM platform_memberships WHERE account_id = %s ORDER BY user_id, id
                """,
                (account_id,),
            )
            rows = cur.fetchall()
        return [
            Membership(id=r[0], account_id=r[1], user_id=r[2], role_id=r[3], space_id=r[4],
                       organization_id=r[5], status=r[6], created_at=_iso(r[7]))
            for r in rows
        ]

    def upsert_consent_record(self, record: ConsentRecord) -> ConsentRecord:
        self._validate_governance_scope(record.account_id, record.space_id)
        with self._conn(account_id=record.account_id, space_id=record.space_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_consent_records
                (id, account_id, subject_ref, purpose, status, space_id, source, captured_by, withdrawn_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    subject_ref = EXCLUDED.subject_ref, purpose = EXCLUDED.purpose,
                    status = EXCLUDED.status, space_id = EXCLUDED.space_id,
                    source = EXCLUDED.source, captured_by = EXCLUDED.captured_by,
                    withdrawn_at = EXCLUDED.withdrawn_at
                RETURNING id, account_id, subject_ref, purpose, status, space_id, source, captured_by, withdrawn_at, created_at
                """,
                (record.id, record.account_id, record.subject_ref, record.purpose, record.status,
                 record.space_id, record.source, record.captured_by, record.withdrawn_at),
            )
            row = cur.fetchone()
            conn.commit()
        return ConsentRecord(id=row[0], account_id=row[1], subject_ref=row[2], purpose=row[3], status=row[4],
                             space_id=row[5], source=row[6], captured_by=row[7], withdrawn_at=row[8],
                             created_at=_iso(row[9]))

    def list_consent_records(self, account_id: str, space_id: str = "") -> List[ConsentRecord]:
        rows = self._list_scope(
            "platform_consent_records",
            "id, account_id, subject_ref, purpose, status, space_id, source, captured_by, withdrawn_at, created_at",
            account_id,
            space_id,
            "created_at, id",
        )
        return [
            ConsentRecord(id=r[0], account_id=r[1], subject_ref=r[2], purpose=r[3], status=r[4],
                          space_id=r[5], source=r[6], captured_by=r[7], withdrawn_at=r[8], created_at=_iso(r[9]))
            for r in rows
        ]

    def upsert_retention_policy(self, policy: RetentionPolicy) -> RetentionPolicy:
        self._validate_governance_scope(policy.account_id, policy.space_id)
        if policy.duration_days < 0:
            raise ValueError("retention duration must be non-negative")
        with self._conn(account_id=policy.account_id, space_id=policy.space_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_retention_policies
                (id, account_id, domain, record_type, action, duration_days, legal_basis, space_id, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    domain = EXCLUDED.domain, record_type = EXCLUDED.record_type,
                    action = EXCLUDED.action, duration_days = EXCLUDED.duration_days,
                    legal_basis = EXCLUDED.legal_basis, space_id = EXCLUDED.space_id,
                    status = EXCLUDED.status
                RETURNING id, account_id, domain, record_type, action, duration_days, legal_basis, space_id, status, created_at
                """,
                (policy.id, policy.account_id, policy.domain, policy.record_type, policy.action,
                 policy.duration_days, policy.legal_basis, policy.space_id, policy.status),
            )
            row = cur.fetchone()
            conn.commit()
        return RetentionPolicy(id=row[0], account_id=row[1], domain=row[2], record_type=row[3], action=row[4],
                               duration_days=int(row[5]), legal_basis=row[6], space_id=row[7], status=row[8],
                               created_at=_iso(row[9]))

    def list_retention_policies(self, account_id: str, space_id: str = "") -> List[RetentionPolicy]:
        rows = self._list_scope(
            "platform_retention_policies",
            "id, account_id, domain, record_type, action, duration_days, legal_basis, space_id, status, created_at",
            account_id,
            space_id,
            "domain, record_type, id",
        )
        return [
            RetentionPolicy(id=r[0], account_id=r[1], domain=r[2], record_type=r[3], action=r[4],
                            duration_days=int(r[5]), legal_basis=r[6], space_id=r[7], status=r[8],
                            created_at=_iso(r[9]))
            for r in rows
        ]

    def record_data_access(self, event: DataAccessEvent) -> DataAccessEvent:
        self._validate_governance_scope(event.account_id, event.space_id)
        with self._conn(account_id=event.account_id, space_id=event.space_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_data_access_events
                (id, account_id, actor_id, actor_type, action, target_type, target_id,
                 space_id, app_id, purpose, decision, meta)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, account_id, actor_id, actor_type, action, target_type, target_id,
                    space_id, app_id, purpose, decision, meta, created_at
                """,
                (event.id, event.account_id, event.actor_id, event.actor_type, event.action,
                 event.target_type, event.target_id, event.space_id, event.app_id, event.purpose,
                 event.decision, json.dumps(event.meta)),
            )
            row = cur.fetchone()
            conn.commit()
        return DataAccessEvent(id=row[0], account_id=row[1], actor_id=row[2], actor_type=row[3], action=row[4],
                               target_type=row[5], target_id=row[6], space_id=row[7], app_id=row[8],
                               purpose=row[9], decision=row[10], meta=_json(row[11]), created_at=_iso(row[12]))

    def list_data_access_events(self, account_id: str, space_id: str = "") -> List[DataAccessEvent]:
        rows = self._list_scope(
            "platform_data_access_events",
            "id, account_id, actor_id, actor_type, action, target_type, target_id, space_id, app_id, purpose, decision, meta, created_at",
            account_id,
            space_id,
            "created_at, id",
        )
        return [
            DataAccessEvent(id=r[0], account_id=r[1], actor_id=r[2], actor_type=r[3], action=r[4],
                            target_type=r[5], target_id=r[6], space_id=r[7], app_id=r[8],
                            purpose=r[9], decision=r[10], meta=_json(r[11]), created_at=_iso(r[12]))
            for r in rows
        ]

    def upsert_processor(self, processor: ProcessorRegistration) -> ProcessorRegistration:
        if processor.account_id and not self.get_account(processor.account_id):
            raise ValueError(f"unknown account: {processor.account_id}")
        with self._conn(account_id=processor.account_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_processor_register
                (id, name, category, region, dpa_status, transfer_mechanism, account_id, status, meta)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name, category = EXCLUDED.category, region = EXCLUDED.region,
                    dpa_status = EXCLUDED.dpa_status, transfer_mechanism = EXCLUDED.transfer_mechanism,
                    account_id = EXCLUDED.account_id, status = EXCLUDED.status, meta = EXCLUDED.meta
                RETURNING id, name, category, region, dpa_status, transfer_mechanism, account_id, status, meta, created_at
                """,
                (processor.id, processor.name, processor.category, processor.region, processor.dpa_status,
                 processor.transfer_mechanism, processor.account_id, processor.status, json.dumps(processor.meta)),
            )
            row = cur.fetchone()
            conn.commit()
        return ProcessorRegistration(id=row[0], name=row[1], category=row[2], region=row[3], dpa_status=row[4],
                                     transfer_mechanism=row[5], account_id=row[6], status=row[7],
                                     meta=_json(row[8]), created_at=_iso(row[9]))

    def list_processors(self, account_id: str = "") -> List[ProcessorRegistration]:
        rows = self._list_register("platform_processor_register", "dpa_status", account_id)
        return [
            ProcessorRegistration(id=r[0], name=r[1], category=r[2], region=r[3], dpa_status=r[4],
                                  transfer_mechanism=r[5], account_id=r[6], status=r[7],
                                  meta=_json(r[8]), created_at=_iso(r[9]))
            for r in rows
        ]

    def upsert_provider(self, provider: ProviderRegistration) -> ProviderRegistration:
        if provider.account_id and not self.get_account(provider.account_id):
            raise ValueError(f"unknown account: {provider.account_id}")
        with self._conn(account_id=provider.account_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_provider_register
                (id, name, category, region, dpia_status, transfer_mechanism, account_id, status, meta)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name, category = EXCLUDED.category, region = EXCLUDED.region,
                    dpia_status = EXCLUDED.dpia_status, transfer_mechanism = EXCLUDED.transfer_mechanism,
                    account_id = EXCLUDED.account_id, status = EXCLUDED.status, meta = EXCLUDED.meta
                RETURNING id, name, category, region, dpia_status, transfer_mechanism, account_id, status, meta, created_at
                """,
                (provider.id, provider.name, provider.category, provider.region, provider.dpia_status,
                 provider.transfer_mechanism, provider.account_id, provider.status, json.dumps(provider.meta)),
            )
            row = cur.fetchone()
            conn.commit()
        return ProviderRegistration(id=row[0], name=row[1], category=row[2], region=row[3], dpia_status=row[4],
                                    transfer_mechanism=row[5], account_id=row[6], status=row[7],
                                    meta=_json(row[8]), created_at=_iso(row[9]))

    def list_providers(self, account_id: str = "") -> List[ProviderRegistration]:
        rows = self._list_register("platform_provider_register", "dpia_status", account_id)
        return [
            ProviderRegistration(id=r[0], name=r[1], category=r[2], region=r[3], dpia_status=r[4],
                                 transfer_mechanism=r[5], account_id=r[6], status=r[7],
                                 meta=_json(r[8]), created_at=_iso(r[9]))
            for r in rows
        ]

    def upsert_credential_metadata(self, credential: CredentialMetadata) -> CredentialMetadata:
        if not self.get_account(credential.account_id):
            raise ValueError(f"unknown account: {credential.account_id}")
        with self._conn(account_id=credential.account_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_credential_metadata
                (id, account_id, provider, app_id, secret_ref, status, rotated_at, last_verified_at, meta)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    provider = EXCLUDED.provider, app_id = EXCLUDED.app_id,
                    secret_ref = EXCLUDED.secret_ref, status = EXCLUDED.status,
                    rotated_at = EXCLUDED.rotated_at, last_verified_at = EXCLUDED.last_verified_at,
                    meta = EXCLUDED.meta
                RETURNING id, account_id, provider, app_id, secret_ref, status, rotated_at, last_verified_at, meta, created_at
                """,
                (credential.id, credential.account_id, credential.provider, credential.app_id,
                 credential.secret_ref, credential.status, credential.rotated_at,
                 credential.last_verified_at, json.dumps(credential.meta)),
            )
            row = cur.fetchone()
            conn.commit()
        return CredentialMetadata(id=row[0], account_id=row[1], provider=row[2], app_id=row[3], secret_ref=row[4],
                                  status=row[5], rotated_at=row[6], last_verified_at=row[7],
                                  meta=_json(row[8]), created_at=_iso(row[9]))

    def list_credential_metadata(self, account_id: str) -> List[CredentialMetadata]:
        with self._conn(account_id=account_id) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, account_id, provider, app_id, secret_ref, status, rotated_at, last_verified_at, meta, created_at
                FROM platform_credential_metadata
                WHERE account_id = %s
                ORDER BY provider, id
                """,
                (account_id,),
            )
            rows = cur.fetchall()
        return [
            CredentialMetadata(id=r[0], account_id=r[1], provider=r[2], app_id=r[3], secret_ref=r[4],
                               status=r[5], rotated_at=r[6], last_verified_at=r[7],
                               meta=_json(r[8]), created_at=_iso(r[9]))
            for r in rows
        ]

    def delete_governance_by_scope(self, account_id: str, space_id: str = "") -> dict[str, int]:
        counts = {}
        with self._conn(account_id=account_id, space_id=space_id) as conn, conn.cursor() as cur:
            if not space_id:
                cur.execute("DELETE FROM platform_organizations WHERE account_id = %s", (account_id,))
                counts["organizations"] = cur.rowcount
                cur.execute("DELETE FROM platform_credential_metadata WHERE account_id = %s", (account_id,))
                counts["credential_metadata"] = cur.rowcount
            else:
                counts["organizations"] = 0
                counts["credential_metadata"] = 0
            for key, table in [
                ("memberships", "platform_memberships"),
                ("consent_records", "platform_consent_records"),
                ("retention_policies", "platform_retention_policies"),
                ("data_access_events", "platform_data_access_events"),
            ]:
                if space_id:
                    cur.execute(f"DELETE FROM {table} WHERE account_id = %s AND space_id = %s", (account_id, space_id))
                else:
                    cur.execute(f"DELETE FROM {table} WHERE account_id = %s", (account_id,))
                counts[key] = cur.rowcount
            conn.commit()
        return counts

    def _validate_governance_scope(self, account_id: str, space_id: str = "") -> None:
        if not self.get_account(account_id):
            raise ValueError(f"unknown account: {account_id}")
        if space_id:
            space = self.get_space(space_id)
            if not space or space.account_id != account_id:
                raise ValueError(f"space is not in this account: {space_id}")

    def _list_scope(self, table: str, columns: str, account_id: str, space_id: str, order: str):
        clauses = ["account_id = %s"]
        params = [account_id]
        if space_id:
            clauses.append("space_id = %s")
            params.append(space_id)
        with self._conn(account_id=account_id, space_id=space_id) as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {columns} FROM {table} WHERE {' AND '.join(clauses)} ORDER BY {order}",
                tuple(params),
            )
            return cur.fetchall()

    def _list_register(self, table: str, status_column: str, account_id: str):
        where = ""
        params = ()
        if account_id:
            where = "WHERE account_id = '' OR account_id = %s"
            params = (account_id,)
        with self._conn(account_id=account_id, admin=not bool(account_id)) as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, name, category, region, {status_column}, transfer_mechanism, account_id, status, meta, created_at
                FROM {table}
                {where}
                ORDER BY name, id
                """,
                params,
            )
            return cur.fetchall()
