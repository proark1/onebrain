"""Add a record-only, two-person customer teardown approval protocol.

The table intentionally stores review evidence and a hash of a short-lived
approval nonce only.  It has no infrastructure identifier or execution state
that could be used to delete customer resources.

Revision ID: 0028_customer_teardown_protocol
Revises: 0027_ai_agent_run_leases
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op


revision = "0028_customer_teardown_protocol"
down_revision = "0027_ai_agent_run_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS control_customer_teardown_requests (
            id TEXT PRIMARY KEY,
            deployment_id TEXT NOT NULL
                REFERENCES control_deployments(id) ON DELETE RESTRICT,
            account_id TEXT NOT NULL,
            nonce_hash TEXT NOT NULL
                CHECK (nonce_hash ~ '^[0-9a-f]{64}$'),
            nonce_expires_at TIMESTAMPTZ NOT NULL,
            legal_hold_evidence_ref TEXT NOT NULL
                CHECK (btrim(legal_hold_evidence_ref) <> ''),
            backup_retention_evidence_ref TEXT NOT NULL
                CHECK (btrim(backup_retention_evidence_ref) <> ''),
            requested_by TEXT NOT NULL
                CHECK (btrim(requested_by) <> ''),
            approver_ids JSONB NOT NULL DEFAULT '[]'::jsonb
                CHECK (jsonb_typeof(approver_ids) = 'array')
                CHECK (jsonb_array_length(approver_ids) <= 2)
                CHECK (jsonb_array_length(approver_ids) < 2
                    OR approver_ids ->> 0 <> approver_ids ->> 1)
                CHECK (NOT (approver_ids ? requested_by)),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'execution_disabled', 'expired')),
            execution_result TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            CONSTRAINT control_customer_teardown_requests_terminal_state CHECK (
                (status = 'pending' AND execution_result = '' AND completed_at IS NULL)
                OR (
                    status IN ('execution_disabled', 'expired')
                    AND execution_result = 'execution_disabled: no customer resources were deleted'
                    AND completed_at IS NOT NULL
                )
            ),
            CONSTRAINT control_customer_teardown_requests_two_person_terminal CHECK (
                status <> 'execution_disabled' OR jsonb_array_length(approver_ids) = 2
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS control_customer_teardown_requests_deployment_idx "
        "ON control_customer_teardown_requests (deployment_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS control_customer_teardown_requests_pending_expiry_idx "
        "ON control_customer_teardown_requests (nonce_expires_at) WHERE status = 'pending'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS control_customer_teardown_requests_pending_expiry_idx")
    op.execute("DROP INDEX IF EXISTS control_customer_teardown_requests_deployment_idx")
    op.execute("DROP TABLE IF EXISTS control_customer_teardown_requests")
