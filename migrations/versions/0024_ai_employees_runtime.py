"""Persist the optional AI Employees module runtime.

Revision ID: 0024_ai_employees_runtime
Revises: 0023_kpi_dashboard_data
Create Date: 2026-07-16
"""

from __future__ import annotations

from alembic import op


revision = "0024_ai_employees_runtime"
down_revision = "0023_kpi_dashboard_data"
branch_labels = None
depends_on = None


AI_EMPLOYEE_TABLES = (
    "ai_employee_versions",
    "ai_employee_profiles",
    "ai_employee_model_policies",
    "ai_employee_conversations",
    "ai_employee_messages",
    "ai_missions",
    "ai_mission_participants",
    "ai_agent_runs",
    "ai_employee_memories",
    "ai_connector_bindings",
    "ai_action_proposals",
)


def _scope_columns() -> str:
    return """
        tenant_id TEXT NOT NULL,
        account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
        space_id TEXT NOT NULL REFERENCES platform_spaces(id) ON DELETE CASCADE
    """


def _rls_policy_sql(table: str) -> str:
    return f"""
        CREATE POLICY onebrain_{table}_scope ON {table}
        USING (
            _onebrain_rls_admin()
            OR (
                tenant_id = current_setting('app.tenant_id', true)
                AND account_id = current_setting('app.account_id', true)
                AND (
                    current_setting('app.space_id', true) = ''
                    OR space_id = current_setting('app.space_id', true)
                )
            )
        )
        WITH CHECK (
            _onebrain_rls_admin()
            OR (
                tenant_id = current_setting('app.tenant_id', true)
                AND account_id = current_setting('app.account_id', true)
                AND (
                    current_setting('app.space_id', true) = ''
                    OR space_id = current_setting('app.space_id', true)
                )
            )
        )
    """


def upgrade() -> None:
    scope = _scope_columns()
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS ai_employee_versions (
            id TEXT PRIMARY KEY,
            {scope},
            employee_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version > 0),
            state TEXT NOT NULL CHECK (state IN ('draft', 'published')),
            payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            checksum TEXT NOT NULL CHECK (char_length(checksum) = 64),
            author_id TEXT NOT NULL,
            base_version_id TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            published_at TIMESTAMPTZ,
            CONSTRAINT ai_employee_versions_scope_id_unique UNIQUE (id, tenant_id, account_id, space_id),
            CONSTRAINT ai_employee_versions_number_unique
                UNIQUE (tenant_id, account_id, space_id, employee_id, version)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ai_employee_versions_employee_idx "
        "ON ai_employee_versions (tenant_id, account_id, space_id, employee_id, version DESC)"
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS ai_employee_profiles (
            id TEXT PRIMARY KEY,
            {scope},
            employee_id TEXT NOT NULL,
            role TEXT NOT NULL,
            department TEXT NOT NULL,
            pod TEXT NOT NULL,
            reports_to TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (status IN ('active', 'paused')),
            default_version_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ai_employee_profiles_scope_id_unique UNIQUE (id, tenant_id, account_id, space_id),
            CONSTRAINT ai_employee_profiles_employee_unique
                UNIQUE (tenant_id, account_id, space_id, employee_id),
            CONSTRAINT ai_employee_profiles_version_scope_fk
                FOREIGN KEY (default_version_id, tenant_id, account_id, space_id)
                REFERENCES ai_employee_versions(id, tenant_id, account_id, space_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ai_employee_profiles_roster_idx "
        "ON ai_employee_profiles (tenant_id, account_id, space_id, status, pod, employee_id)"
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS ai_employee_model_policies (
            id TEXT PRIMARY KEY,
            {scope},
            employee_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version > 0),
            provider TEXT NOT NULL CHECK (provider IN ('gemini', 'anthropic', 'local')),
            model TEXT NOT NULL,
            task_overrides JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            allowed_fallbacks TEXT[] NOT NULL DEFAULT '{{}}',
            data_ceiling TEXT NOT NULL CHECK (data_ceiling IN ('public', 'internal', 'confidential', 'restricted')),
            cost_limit_usd NUMERIC(12,6) NOT NULL DEFAULT 0 CHECK (cost_limit_usd >= 0),
            status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ai_employee_model_policy_unique
                UNIQUE (tenant_id, account_id, space_id, employee_id, version)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ai_employee_model_policy_active_idx "
        "ON ai_employee_model_policies (tenant_id, account_id, space_id, employee_id, status, version DESC)"
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS ai_employee_conversations (
            id TEXT PRIMARY KEY,
            {scope},
            employee_id TEXT NOT NULL,
            human_owner_id TEXT NOT NULL,
            title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 160),
            status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
            character_version_id TEXT NOT NULL,
            model_policy_id TEXT NOT NULL,
            mission_id TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ai_employee_conversations_scope_id_unique UNIQUE (id, tenant_id, account_id, space_id),
            CONSTRAINT ai_employee_conversations_character_scope_fk
                FOREIGN KEY (character_version_id, tenant_id, account_id, space_id)
                REFERENCES ai_employee_versions(id, tenant_id, account_id, space_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ai_employee_conversations_owner_idx "
        "ON ai_employee_conversations (tenant_id, account_id, space_id, human_owner_id, updated_at DESC)"
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS ai_employee_messages (
            id TEXT PRIMARY KEY,
            {scope},
            conversation_id TEXT NOT NULL,
            speaker_type TEXT NOT NULL CHECK (speaker_type IN ('human', 'employee', 'system', 'tool')),
            speaker_id TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK (visibility IN ('private', 'shared', 'system')),
            content TEXT NOT NULL,
            citations TEXT[] NOT NULL DEFAULT '{{}}',
            run_id TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ai_employee_messages_conversation_scope_fk
                FOREIGN KEY (conversation_id, tenant_id, account_id, space_id)
                REFERENCES ai_employee_conversations(id, tenant_id, account_id, space_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ai_employee_messages_history_idx "
        "ON ai_employee_messages (conversation_id, created_at, id)"
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS ai_missions (
            id TEXT PRIMARY KEY,
            {scope},
            goal TEXT NOT NULL CHECK (char_length(goal) BETWEEN 1 AND 4000),
            sponsor_id TEXT NOT NULL,
            accountable_employee_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('draft', 'queued', 'running', 'paused', 'completed', 'cancelled', 'failed')),
            phase TEXT NOT NULL,
            token_budget INTEGER NOT NULL CHECK (token_budget > 0),
            time_budget_seconds INTEGER NOT NULL CHECK (time_budget_seconds > 0),
            cost_budget_usd NUMERIC(12,6) NOT NULL CHECK (cost_budget_usd >= 0),
            synthesis_message_id TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ai_missions_scope_id_unique UNIQUE (id, tenant_id, account_id, space_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ai_missions_queue_idx "
        "ON ai_missions (tenant_id, account_id, space_id, status, updated_at)"
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS ai_mission_participants (
            id TEXT PRIMARY KEY,
            {scope},
            mission_id TEXT NOT NULL,
            employee_id TEXT NOT NULL,
            mission_role TEXT NOT NULL,
            character_version_id TEXT NOT NULL,
            model_policy_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'failed', 'completed', 'left')),
            joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ai_mission_participant_unique UNIQUE (mission_id, employee_id),
            CONSTRAINT ai_mission_participants_mission_scope_fk
                FOREIGN KEY (mission_id, tenant_id, account_id, space_id)
                REFERENCES ai_missions(id, tenant_id, account_id, space_id) ON DELETE CASCADE,
            CONSTRAINT ai_mission_participants_character_scope_fk
                FOREIGN KEY (character_version_id, tenant_id, account_id, space_id)
                REFERENCES ai_employee_versions(id, tenant_id, account_id, space_id)
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS ai_agent_runs (
            id TEXT PRIMARY KEY,
            {scope},
            conversation_id TEXT NOT NULL DEFAULT '',
            mission_id TEXT NOT NULL DEFAULT '',
            employee_id TEXT NOT NULL,
            backend TEXT NOT NULL,
            model TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'blocked')),
            input_hash TEXT NOT NULL,
            provider_session_ref TEXT NOT NULL DEFAULT '',
            prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
            completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
            cost_usd NUMERIC(12,6) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
            warning TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            CONSTRAINT ai_agent_runs_idempotency_unique
                UNIQUE (tenant_id, account_id, space_id, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ai_agent_runs_queue_idx "
        "ON ai_agent_runs (tenant_id, account_id, space_id, status, created_at)"
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS ai_employee_memories (
            id TEXT PRIMARY KEY,
            {scope},
            employee_id TEXT NOT NULL,
            content TEXT NOT NULL,
            source_refs TEXT[] NOT NULL,
            classification TEXT NOT NULL CHECK (classification IN ('public', 'internal', 'confidential', 'restricted')),
            status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'deleted')),
            retention_until TIMESTAMPTZ NOT NULL,
            author_id TEXT NOT NULL,
            approved_by TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            approved_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ai_employee_memories_active_idx "
        "ON ai_employee_memories (tenant_id, account_id, space_id, employee_id, status, retention_until)"
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS ai_connector_bindings (
            id TEXT PRIMARY KEY,
            {scope},
            provider TEXT NOT NULL,
            credential_ref TEXT NOT NULL CHECK (credential_ref LIKE 'secret://%'),
            resource_type TEXT NOT NULL,
            resource_ids TEXT[] NOT NULL DEFAULT '{{}}',
            employee_ids TEXT[] NOT NULL DEFAULT '{{}}',
            capabilities TEXT[] NOT NULL DEFAULT '{{}}',
            status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'revoked', 'error')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ai_connector_bindings_active_idx "
        "ON ai_connector_bindings (tenant_id, account_id, space_id, provider, status)"
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS ai_action_proposals (
            id TEXT PRIMARY KEY,
            {scope},
            mission_id TEXT NOT NULL DEFAULT '',
            conversation_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            employee_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            target_system TEXT NOT NULL,
            risk_level TEXT NOT NULL CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
            classification TEXT NOT NULL CHECK (classification IN ('public', 'internal', 'confidential', 'restricted')),
            actionability TEXT NOT NULL CHECK (actionability IN ('answer_only', 'draft_only', 'approval_required', 'automation_allowed')),
            source_record_ids TEXT[] NOT NULL,
            payload_summary TEXT NOT NULL,
            payload JSONB NOT NULL,
            payload_hash TEXT NOT NULL CHECK (char_length(payload_hash) = 64),
            required_approver_role TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('draft', 'proposed', 'approved', 'rejected', 'changes_requested', 'expired', 'blocked_by_policy', 'executed', 'execution_failed', 'duplicate')),
            requires_approval BOOLEAN NOT NULL,
            reason TEXT NOT NULL,
            approved_by TEXT NOT NULL DEFAULT '',
            approved_at TIMESTAMPTZ,
            execution_ref TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ai_action_proposals_idempotency_unique
                UNIQUE (tenant_id, account_id, space_id, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ai_action_proposals_queue_idx "
        "ON ai_action_proposals (tenant_id, account_id, space_id, status, expires_at, created_at)"
    )

    for table in AI_EMPLOYEE_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS onebrain_{table}_scope ON {table}")
        op.execute(_rls_policy_sql(table))


def downgrade() -> None:
    for table in reversed(AI_EMPLOYEE_TABLES):
        op.execute(f"DROP POLICY IF EXISTS onebrain_{table}_scope ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"DROP TABLE IF EXISTS {table}")
