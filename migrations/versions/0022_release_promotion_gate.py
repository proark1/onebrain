"""Development release gate and durable customer rollout policy.

Revision ID: 0022_release_promotion_gate
Revises: 0021_phase5_fleet_secrets
Create Date: 2026-07-13
"""

from __future__ import annotations

from alembic import op


revision = "0022_release_promotion_gate"
down_revision = "0021_phase5_fleet_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE control_deployments "
        "ADD COLUMN IF NOT EXISTS is_release_gate BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE control_deployments "
        "ADD COLUMN IF NOT EXISTS current_version_deployed_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE control_deployments "
        "ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE control_deployments "
        "ADD COLUMN IF NOT EXISTS last_heartbeat_healthy BOOLEAN"
    )
    op.execute(
        "ALTER TABLE control_deployments "
        "ADD COLUMN IF NOT EXISTS last_reported_version TEXT NOT NULL DEFAULT ''"
    )
    op.execute(
        "ALTER TABLE control_deployments "
        "ADD COLUMN IF NOT EXISTS last_reported_migration TEXT NOT NULL DEFAULT ''"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS control_deployments_one_active_gate_idx "
        "ON control_deployments (is_release_gate) "
        "WHERE is_release_gate = true AND status = 'active'"
    )

    # Best-effort historical install date. A matching completed rollout is the
    # strongest signal; a successful provisioning run is the fallback.
    op.execute(
        """
        UPDATE control_deployments AS deployment
        SET current_version_deployed_at = COALESCE(
            (
                SELECT MAX(rollout.completed_at)
                FROM control_rollouts AS rollout
                WHERE rollout.deployment_id = deployment.id
                  AND rollout.status = 'success'
                  AND rollout.target_version = deployment.current_version
            ),
            (
                SELECT MAX(run.completed_at)
                FROM provisioning_runs AS run
                WHERE run.deployment_id = deployment.id
                  AND run.status = 'success'
                  AND COALESCE(
                        run.result_payload->>'target_version',
                        run.result_payload->>'initial_version',
                        ''
                      ) = deployment.current_version
            )
        )
        WHERE deployment.current_version <> ''
          AND deployment.current_version_deployed_at IS NULL
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS control_release_promotions (
            release_version       TEXT PRIMARY KEY
                                  REFERENCES control_release_manifests(version) ON DELETE CASCADE,
            state                 TEXT NOT NULL DEFAULT 'dev_pending'
                                  CHECK (state IN (
                                      'dev_pending', 'dev_deploying', 'dev_verified',
                                      'dev_failed', 'customer_approved',
                                      'customer_paused', 'yanked'
                                  )),
            gate_deployment_id    TEXT REFERENCES control_deployments(id) ON DELETE RESTRICT,
            dev_signature         TEXT NOT NULL DEFAULT '',
            dev_signing_key_id    TEXT NOT NULL DEFAULT '',
            dev_rollout_id        TEXT REFERENCES control_rollouts(id) ON DELETE SET NULL,
            dev_attempt_id        TEXT NOT NULL DEFAULT '',
            dev_started_at        TIMESTAMPTZ,
            dev_completed_at      TIMESTAMPTZ,
            dev_verified_at       TIMESTAMPTZ,
            customer_approved_at  TIMESTAMPTZ,
            customer_approved_by  TEXT NOT NULL DEFAULT '',
            customer_paused_at    TIMESTAMPTZ,
            customer_paused_reason TEXT NOT NULL DEFAULT '',
            yanked_at             TIMESTAMPTZ,
            failure_reason        TEXT NOT NULL DEFAULT '',
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS control_release_promotions_state_idx "
        "ON control_release_promotions (state, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS control_release_promotion_events (
            id               BIGSERIAL PRIMARY KEY,
            release_version  TEXT NOT NULL
                             REFERENCES control_release_manifests(version) ON DELETE CASCADE,
            actor            TEXT NOT NULL DEFAULT '',
            action           TEXT NOT NULL,
            from_state       TEXT NOT NULL DEFAULT '',
            to_state         TEXT NOT NULL,
            note             TEXT NOT NULL DEFAULT '',
            metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS control_release_promotion_events_release_idx "
        "ON control_release_promotion_events (release_version, created_at ASC, id ASC)"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION onebrain_reject_promotion_event_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'release promotion events are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS control_release_promotion_events_no_update
        ON control_release_promotion_events
        """
    )
    op.execute(
        """
        CREATE TRIGGER control_release_promotion_events_no_update
        BEFORE UPDATE OR DELETE ON control_release_promotion_events
        FOR EACH ROW EXECUTE FUNCTION onebrain_reject_promotion_event_mutation()
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS control_release_promotion_events_no_truncate
        ON control_release_promotion_events
        """
    )
    op.execute(
        """
        CREATE TRIGGER control_release_promotion_events_no_truncate
        BEFORE TRUNCATE ON control_release_promotion_events
        FOR EACH STATEMENT EXECUTE FUNCTION onebrain_reject_promotion_event_mutation()
        """
    )

    op.execute(
        "ALTER TABLE control_fleet_rollouts "
        "ADD COLUMN IF NOT EXISTS ring_batch_size INTEGER NOT NULL DEFAULT 1"
    )
    op.execute(
        "ALTER TABLE control_fleet_rollouts "
        "ADD COLUMN IF NOT EXISTS only_deployment_ids JSONB NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute(
        "ALTER TABLE control_fleet_rollouts "
        "ADD COLUMN IF NOT EXISTS include_manual_pinned BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE control_fleet_rollouts DROP COLUMN IF EXISTS include_manual_pinned"
    )
    op.execute(
        "ALTER TABLE control_fleet_rollouts DROP COLUMN IF EXISTS only_deployment_ids"
    )
    op.execute(
        "ALTER TABLE control_fleet_rollouts DROP COLUMN IF EXISTS ring_batch_size"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS control_release_promotion_events_no_truncate "
        "ON control_release_promotion_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS control_release_promotion_events_no_update "
        "ON control_release_promotion_events"
    )
    op.execute("DROP FUNCTION IF EXISTS onebrain_reject_promotion_event_mutation()")
    op.execute("DROP INDEX IF EXISTS control_release_promotion_events_release_idx")
    op.execute("DROP TABLE IF EXISTS control_release_promotion_events")
    op.execute("DROP INDEX IF EXISTS control_release_promotions_state_idx")
    op.execute("DROP TABLE IF EXISTS control_release_promotions")
    op.execute("DROP INDEX IF EXISTS control_deployments_one_active_gate_idx")
    op.execute("ALTER TABLE control_deployments DROP COLUMN IF EXISTS last_reported_migration")
    op.execute("ALTER TABLE control_deployments DROP COLUMN IF EXISTS last_reported_version")
    op.execute("ALTER TABLE control_deployments DROP COLUMN IF EXISTS last_heartbeat_healthy")
    op.execute("ALTER TABLE control_deployments DROP COLUMN IF EXISTS last_heartbeat_at")
    op.execute("ALTER TABLE control_deployments DROP COLUMN IF EXISTS current_version_deployed_at")
    op.execute("ALTER TABLE control_deployments DROP COLUMN IF EXISTS is_release_gate")
