"""Fleet decommission executor: deployment tombstone + teardown executor states.

Adds ``control_deployments.removed_at`` (the decommission tombstone; a removed
deployment is filtered out of the fleet views but kept for audit + erasure-manifest
history) and widens the teardown-request CHECK constraints so the executor
lifecycle can be persisted: reaching the approval threshold now yields ``approved``
(executable), and execution moves the row to ``executed`` or ``execution_failed``.

Migration 0028 pinned ``status`` to pending/execution_disabled/expired via a mix of
NAMED and column-inline (auto-named) CHECK constraints, and barred self-approval at
the DB level. Postgres rejects the new statuses until those checks are widened, so we
drop EVERY check on the table and recreate a complete, explicitly-named set. The
self-approval bar is intentionally NOT recreated: self-approval is now a
settings-gated POLICY enforced in application code (``teardown_allow_self_approval``),
defaulting to disallowed — the DB check can't read that setting.

Revision ID: 0035_fleet_decommission_tombstone
Revises: 0034_drive_malware_quarantine
Create Date: 2026-07-22
"""

from __future__ import annotations

from alembic import op


revision = "0035_fleet_decommission_tombstone"
down_revision = "0034_drive_malware_quarantine"
branch_labels = None
depends_on = None


_DISABLED_RESULT = "execution_disabled: no customer resources were deleted"

_DROP_ALL_TEARDOWN_CHECKS = """
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'control_customer_teardown_requests'::regclass
          AND contype = 'c'
    LOOP
        EXECUTE 'ALTER TABLE control_customer_teardown_requests DROP CONSTRAINT '
                || quote_ident(r.conname);
    END LOOP;
END $$;
"""

# The invariant checks shared by both the old and new constraint sets (structural
# shape of nonce/evidence/approvers). Only the status/terminal/self-approval checks
# differ between upgrade and downgrade.
_SHARED_TEARDOWN_CHECKS = """
    ADD CONSTRAINT control_customer_teardown_requests_nonce_hash_check
        CHECK (nonce_hash ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT control_customer_teardown_requests_legal_hold_evidence_check
        CHECK (btrim(legal_hold_evidence_ref) <> ''),
    ADD CONSTRAINT control_customer_teardown_requests_backup_retention_evidence_check
        CHECK (btrim(backup_retention_evidence_ref) <> ''),
    ADD CONSTRAINT control_customer_teardown_requests_requested_by_check
        CHECK (btrim(requested_by) <> ''),
    ADD CONSTRAINT control_customer_teardown_requests_approvers_array_check
        CHECK (jsonb_typeof(approver_ids) = 'array'),
    ADD CONSTRAINT control_customer_teardown_requests_approvers_len_check
        CHECK (jsonb_array_length(approver_ids) <= 2),
    ADD CONSTRAINT control_customer_teardown_requests_approvers_distinct_check
        CHECK (jsonb_array_length(approver_ids) < 2
               OR approver_ids ->> 0 <> approver_ids ->> 1)
"""


def upgrade() -> None:
    # 1) Deployment tombstone.
    op.execute("ALTER TABLE control_deployments ADD COLUMN IF NOT EXISTS removed_at TIMESTAMPTZ")
    op.execute(
        "CREATE INDEX IF NOT EXISTS control_deployments_active_idx "
        "ON control_deployments (lower(customer_name), id) WHERE removed_at IS NULL"
    )

    # 2) Widen the teardown CHECK constraints for the executor lifecycle.
    op.execute(_DROP_ALL_TEARDOWN_CHECKS)
    op.execute(
        f"""
        ALTER TABLE control_customer_teardown_requests
            {_SHARED_TEARDOWN_CHECKS},
            ADD CONSTRAINT control_customer_teardown_requests_status_check
                CHECK (status IN ('pending', 'execution_disabled', 'expired',
                                  'approved', 'executed', 'execution_failed')),
            ADD CONSTRAINT control_customer_teardown_requests_legacy_two_person_terminal
                CHECK (status <> 'execution_disabled'
                       OR jsonb_array_length(approver_ids) = 2),
            ADD CONSTRAINT control_customer_teardown_requests_terminal_state CHECK (
                (status IN ('pending', 'approved')
                    AND execution_result = '' AND completed_at IS NULL)
                OR (status IN ('execution_disabled', 'expired')
                    AND execution_result = '{_DISABLED_RESULT}'
                    AND completed_at IS NOT NULL)
                OR (status IN ('executed', 'execution_failed')
                    AND btrim(execution_result) <> '' AND completed_at IS NOT NULL)
            )
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS control_deployments_active_idx")
    op.execute("ALTER TABLE control_deployments DROP COLUMN IF EXISTS removed_at")

    # Restore the 0028 record-only constraint set (this fails if any executor-state
    # rows exist — a deliberate downgrade guard, not an oversight).
    op.execute(_DROP_ALL_TEARDOWN_CHECKS)
    op.execute(
        f"""
        ALTER TABLE control_customer_teardown_requests
            {_SHARED_TEARDOWN_CHECKS},
            ADD CONSTRAINT control_customer_teardown_requests_no_self_approval_check
                CHECK (NOT (approver_ids ? requested_by)),
            ADD CONSTRAINT control_customer_teardown_requests_status_check
                CHECK (status IN ('pending', 'execution_disabled', 'expired')),
            ADD CONSTRAINT control_customer_teardown_requests_terminal_state CHECK (
                (status = 'pending' AND execution_result = '' AND completed_at IS NULL)
                OR (status IN ('execution_disabled', 'expired')
                    AND execution_result = '{_DISABLED_RESULT}'
                    AND completed_at IS NOT NULL)
            ),
            ADD CONSTRAINT control_customer_teardown_requests_two_person_terminal CHECK (
                status <> 'execution_disabled' OR jsonb_array_length(approver_ids) = 2
            )
        """
    )
