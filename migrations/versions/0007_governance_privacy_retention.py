"""Governance records, privacy metadata, and retention runs.

Revision ID: 0007_governance_privacy_retention
Revises: 0006_provisioning_runs
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op


revision = "0007_governance_privacy_retention"
down_revision = "0006_provisioning_runs"
branch_labels = None
depends_on = None

GOVERNANCE_TABLES = (
    "platform_organizations",
    "platform_memberships",
    "platform_consent_records",
    "platform_retention_policies",
    "platform_data_access_events",
    "platform_processor_register",
    "platform_provider_register",
    "platform_credential_metadata",
    "retention_runs",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_organizations (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS platform_organizations_account_idx ON platform_organizations (account_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_memberships (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            role_id TEXT NOT NULL,
            space_id TEXT NOT NULL DEFAULT '',
            organization_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS platform_memberships_account_idx ON platform_memberships (account_id, user_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_consent_records (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            subject_ref TEXT NOT NULL,
            purpose TEXT NOT NULL,
            status TEXT NOT NULL,
            space_id TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            captured_by TEXT NOT NULL DEFAULT '',
            withdrawn_at TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS platform_consent_scope_idx ON platform_consent_records (account_id, space_id, purpose)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_retention_policies (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            domain TEXT NOT NULL,
            record_type TEXT NOT NULL,
            action TEXT NOT NULL,
            duration_days INTEGER NOT NULL,
            legal_basis TEXT NOT NULL,
            space_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS platform_retention_scope_idx ON platform_retention_policies (account_id, space_id, domain, status)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_data_access_events (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            actor_id TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            space_id TEXT NOT NULL DEFAULT '',
            app_id TEXT NOT NULL DEFAULT '',
            purpose TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL DEFAULT '',
            meta JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS platform_data_access_scope_idx ON platform_data_access_events (account_id, space_id, created_at DESC)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_processor_register (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            region TEXT NOT NULL,
            dpa_status TEXT NOT NULL,
            transfer_mechanism TEXT NOT NULL DEFAULT '',
            account_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            meta JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS platform_processor_scope_idx ON platform_processor_register (account_id, status)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_provider_register (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            region TEXT NOT NULL,
            dpia_status TEXT NOT NULL,
            transfer_mechanism TEXT NOT NULL DEFAULT '',
            account_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            meta JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS platform_provider_scope_idx ON platform_provider_register (account_id, status)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_credential_metadata (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            app_id TEXT NOT NULL,
            secret_ref TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            rotated_at TEXT NOT NULL DEFAULT '',
            last_verified_at TEXT NOT NULL DEFAULT '',
            meta JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS platform_credential_metadata_account_idx ON platform_credential_metadata (account_id, provider)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS retention_runs (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            space_id TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL DEFAULT '',
            dry_run BOOLEAN NOT NULL DEFAULT true,
            status TEXT NOT NULL,
            result JSONB NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS retention_runs_scope_idx ON retention_runs (account_id, space_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS retention_runs_scope_idx")
    op.execute("DROP TABLE IF EXISTS retention_runs")
    op.execute("DROP INDEX IF EXISTS platform_credential_metadata_account_idx")
    op.execute("DROP TABLE IF EXISTS platform_credential_metadata")
    op.execute("DROP INDEX IF EXISTS platform_provider_scope_idx")
    op.execute("DROP TABLE IF EXISTS platform_provider_register")
    op.execute("DROP INDEX IF EXISTS platform_processor_scope_idx")
    op.execute("DROP TABLE IF EXISTS platform_processor_register")
    op.execute("DROP INDEX IF EXISTS platform_data_access_scope_idx")
    op.execute("DROP TABLE IF EXISTS platform_data_access_events")
    op.execute("DROP INDEX IF EXISTS platform_retention_scope_idx")
    op.execute("DROP TABLE IF EXISTS platform_retention_policies")
    op.execute("DROP INDEX IF EXISTS platform_consent_scope_idx")
    op.execute("DROP TABLE IF EXISTS platform_consent_records")
    op.execute("DROP INDEX IF EXISTS platform_memberships_account_idx")
    op.execute("DROP TABLE IF EXISTS platform_memberships")
    op.execute("DROP INDEX IF EXISTS platform_organizations_account_idx")
    op.execute("DROP TABLE IF EXISTS platform_organizations")
