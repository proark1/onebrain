"""Postgres row-level-security helpers and enforcement checks."""

from __future__ import annotations


RLS_REQUIRED_TABLES = (
    "chunks",
    "conversations",
    "messages",
    "intake_records",
    "platform_accounts",
    "platform_spaces",
    "platform_app_installations",
    "platform_brand_themes",
    "platform_audit_events",
    "platform_organizations",
    "platform_memberships",
    "platform_consent_records",
    "platform_retention_policies",
    "platform_legal_holds",
    "platform_tombstones",
    "platform_tombstone_acks",
    "platform_data_access_events",
    "platform_processor_register",
    "platform_provider_register",
    "platform_credential_metadata",
    "retention_runs",
    "provisioning_runs",
    "one_time_secret_envelopes",
    "kpi_definitions",
    "kpi_snapshots",
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


RLS_ACCOUNT_ID = "app.account_id"
RLS_SPACE_ID = "app.space_id"
RLS_TENANT_ID = "app.tenant_id"


class PostgresRLSError(RuntimeError):
    """Raised when RLS is required but not enabled for customer-scoped tables."""


def validate_rls_enabled(conn, tables=RLS_REQUIRED_TABLES) -> None:
    missing: list[str] = []
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(
                """
                SELECT relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE oid = to_regclass(%s)
                """,
                (table,),
            )
            row = cur.fetchone()
            if not row or row[0] is not True or row[1] is not True:
                missing.append(table)
    if missing:
        raise PostgresRLSError(
            "Postgres RLS is required but not enabled and forced for: "
            + ", ".join(missing)
        )


def set_rls_scope(
    conn,
    *,
    tenant_id: str = "",
    account_id: str = "",
    space_id: str = "",
) -> None:
    """Set transaction-local RLS scope GUCs for a Postgres connection.

    Policies fail closed when these values are unset. There is deliberately no
    admin/bypass GUC: the RLS admin bypass is bound to the privileged operator DB
    role (see migration 0009_rls_admin_role and _onebrain_rls_admin), which the
    runtime role cannot assume — so it can never self-assert admin. Cross-account
    operator reads connect as the operator DSN instead of setting a flag.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT set_config(%s, %s, true), set_config(%s, %s, true), set_config(%s, %s, true)",
            (
                RLS_TENANT_ID, (tenant_id or "").strip(),
                RLS_ACCOUNT_ID, (account_id or "").strip(),
                RLS_SPACE_ID, (space_id or "").strip(),
            ),
        )
