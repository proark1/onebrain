"""Postgres row-level-security helpers and enforcement checks."""

from __future__ import annotations

import re


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
    "platform_access_groups",
    "platform_access_group_memberships",
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
    "jobs",
    "job_files",
    "drive_folders",
    "drive_files",
    "drive_file_revisions",
    "drive_upload_sessions",
    "drive_revision_malware_scans",
    "drive_malware_runtime_status",
    "drive_malware_activation_state",
    "drive_malware_settings",
)


RLS_ACCOUNT_ID = "app.account_id"
RLS_SPACE_ID = "app.space_id"
RLS_TENANT_ID = "app.tenant_id"


class PostgresRLSError(RuntimeError):
    """Raised when RLS is required but not enabled for customer-scoped tables."""


class PostgresRoleError(PostgresRLSError):
    """Raised when a supposedly restricted runtime role is unsafe or miswired."""


_ROLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


def validate_job_role_configuration(
    settings,
    *,
    require_worker_dsn: bool = False,
) -> None:
    """Validate the role split required by the jobs/job_files RLS policies.

    The API process knows both role names but must not be given the worker
    password.  The worker process calls this with ``require_worker_dsn=True``.
    Role identity is checked against the live server separately after a
    connection is established.
    """
    app_role = str(getattr(settings, "postgres_app_role", "") or "").strip()
    worker_role = str(getattr(settings, "postgres_worker_role", "") or "").strip()
    worker_dsn = str(getattr(settings, "worker_database_url", "") or "").strip()
    app_dsn = str(getattr(settings, "database_url", "") or "").strip()

    errors: list[str] = []
    for env_name, value in (
        ("ONEBRAIN_POSTGRES_APP_ROLE", app_role),
        ("ONEBRAIN_POSTGRES_WORKER_ROLE", worker_role),
    ):
        if not _ROLE_NAME.fullmatch(value):
            errors.append(f"set {env_name} to a simple PostgreSQL login role name")
    if app_role and worker_role and app_role == worker_role:
        errors.append("ONEBRAIN_POSTGRES_APP_ROLE and ONEBRAIN_POSTGRES_WORKER_ROLE must differ")
    if require_worker_dsn:
        if not worker_dsn:
            errors.append("set ONEBRAIN_WORKER_DATABASE_URL on the worker service")
        elif worker_dsn == app_dsn:
            errors.append(
                "ONEBRAIN_WORKER_DATABASE_URL must not equal ONEBRAIN_DATABASE_URL; "
                "the worker needs its own restricted login"
            )
    if errors:
        raise PostgresRoleError("Job queue role configuration is incomplete: " + "; ".join(errors))


def validate_restricted_runtime_role(conn, expected_role: str, *, purpose: str) -> None:
    """Prove a connected app/worker login cannot bypass RLS globally.

    Job policies grant cross-tenant queue access only to the configured worker
    login.  This check rejects a role that is a superuser, has BYPASSRLS, or is
    recognized by the owner-bound admin predicate, preventing an accidental
    owner/operator DSN from turning the queue worker into a global data reader.
    A NOINHERIT login can still use ``SET ROLE`` for a role granted to it, so
    membership paths to superuser or BYPASSRLS roles are rejected as well.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH RECURSIVE reachable_roles(roleid) AS (
                SELECT roleid
                FROM pg_auth_members
                WHERE member = (SELECT oid FROM pg_roles WHERE rolname = current_user)
                UNION
                SELECT membership.roleid
                FROM pg_auth_members membership
                JOIN reachable_roles reachable ON membership.member = reachable.roleid
            )
            SELECT
                current_user,
                r.rolsuper,
                r.rolbypassrls,
                r.rolinherit,
                r.rolcreaterole,
                r.rolcreatedb,
                r.rolreplication,
                _onebrain_rls_admin(),
                EXISTS (
                    SELECT 1
                    FROM reachable_roles reachable
                    JOIN pg_roles delegated ON delegated.oid = reachable.roleid
                    WHERE delegated.rolsuper OR delegated.rolbypassrls
                )
            FROM pg_roles r
            WHERE r.rolname = current_user
            """
        )
        row = cur.fetchone()
    if not row:
        raise PostgresRoleError(f"Unable to verify the PostgreSQL {purpose} role identity.")

    (
        current_role,
        is_superuser,
        has_bypassrls,
        inherits_privileges,
        can_create_role,
        can_create_database,
        can_replicate,
        is_rls_admin,
        can_assume_privileged_role,
    ) = row
    if current_role != expected_role:
        raise PostgresRoleError(
            f"PostgreSQL {purpose} DSN authenticated as {current_role!r}; "
            f"expected {expected_role!r}."
        )
    if (
        is_superuser
        or has_bypassrls
        or inherits_privileges
        or can_create_role
        or can_create_database
        or can_replicate
        or is_rls_admin
        or can_assume_privileged_role
    ):
        raise PostgresRoleError(
            f"PostgreSQL {purpose} role {expected_role!r} must be NOSUPERUSER, "
            "NOCREATEDB, NOCREATEROLE, NOINHERIT, NOBYPASSRLS, NOREPLICATION, "
            "and outside the owner/operator role hierarchy."
        )


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
