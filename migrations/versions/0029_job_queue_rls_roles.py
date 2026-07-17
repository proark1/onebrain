"""Isolate durable job queues between request and worker database roles.

The request role may create a job in its transaction-local tenant/account/space
scope and read its safe status fields, but it cannot select the queued payload
or any uploaded job file.  A separately authenticated worker login is allowed
to claim and complete jobs across tenants without being a general RLS bypass.

Both role names are supplied at migration time so deployments do not rely on a
fixed, globally shared role name.  Operators must create the two non-owner,
NOSUPERUSER, NOBYPASSRLS login roles before upgrading.

Revision ID: 0029_job_queue_rls_roles
Revises: 0028_auth_rate_limits
Create Date: 2026-07-17
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "0029_job_queue_rls_roles"
down_revision = "0028_auth_rate_limits"
branch_labels = None
depends_on = None


APP_ROLE_ENV = "ONEBRAIN_POSTGRES_APP_ROLE"
WORKER_ROLE_ENV = "ONEBRAIN_POSTGRES_WORKER_ROLE"
JOB_TABLES = ("jobs", "job_files")

# Unquoted PostgreSQL identifiers only.  This keeps role interpolation into
# policy and GRANT statements safe and makes the configured login identity
# unambiguous at runtime.
_ROLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")

_APP_JOB_SELECT_COLUMNS = (
    "id, type, status, tenant_id, account_id, space_id, requested_by, result, "
    "error, attempts, created_at, updated_at, completed_at"
)
_APP_JOB_INSERT_COLUMNS = (
    "id, type, status, tenant_id, account_id, space_id, requested_by, payload, max_attempts"
)
_WORKER_JOB_UPDATE_COLUMNS = (
    "status, result, error, attempts, run_after, locked_by, locked_at, lease_token, "
    "lease_expires_at, updated_at, completed_at"
)
_APP_JOB_FILE_INSERT_COLUMNS = "id, job_id, filename, content_type, size_bytes, data"
_APP_DML_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"


def _configured_role(env_name: str) -> str:
    role = os.environ.get(env_name, "").strip()
    if not _ROLE_NAME.fullmatch(role):
        raise RuntimeError(
            f"{env_name} must name a simple PostgreSQL login role before "
            "running the job-queue RLS migration."
        )
    return role


def _ident(role: str) -> str:
    # _configured_role admits only unquoted identifiers.  Quote anyway so a
    # mixed-case or keyword-looking value cannot change statement structure.
    return f'"{role}"'


def _role_exists(role: str) -> None:
    # Policy/GRANT creation should fail with an actionable preflight error, not
    # leave a successfully stamped revision whose intended worker cannot log in
    # or has retained administrative login attributes from a manual setup.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                RAISE EXCEPTION 'Required PostgreSQL role {role} does not exist';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_roles
                WHERE rolname = '{role}'
                  AND (
                      NOT rolcanlogin
                      OR rolsuper
                      OR rolbypassrls
                      OR rolinherit
                      OR rolcreaterole
                      OR rolcreatedb
                      OR rolreplication
                  )
            ) THEN
                RAISE EXCEPTION
                    'Required PostgreSQL role {role} must be LOGIN, NOSUPERUSER, '
                    'NOCREATEDB, NOCREATEROLE, NOINHERIT, NOBYPASSRLS, and NOREPLICATION';
            END IF;
            IF EXISTS (
                WITH RECURSIVE reachable_roles(roleid) AS (
                    SELECT roleid
                    FROM pg_auth_members
                    WHERE member = (SELECT oid FROM pg_roles WHERE rolname = '{role}')
                    UNION
                    SELECT membership.roleid
                    FROM pg_auth_members membership
                    JOIN reachable_roles reachable ON membership.member = reachable.roleid
                )
                SELECT 1
                FROM reachable_roles reachable
                JOIN pg_roles delegated ON delegated.oid = reachable.roleid
                WHERE delegated.rolsuper OR delegated.rolbypassrls
            ) THEN
                RAISE EXCEPTION
                    'Required PostgreSQL role {role} must not be able to assume a superuser or BYPASSRLS role';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_tables
                WHERE schemaname = 'public'
                  AND tablename = 'platform_accounts'
                  AND pg_has_role('{role}', tableowner, 'MEMBER')
            ) THEN
                RAISE EXCEPTION
                    'Required PostgreSQL role {role} must be outside the owner/operator role hierarchy';
            END IF;
        END
        $$
        """
    )


def _scope_predicate(prefix: str = "") -> str:
    def column(name: str) -> str:
        return f"{prefix}{name}"

    return f"""
        {column('tenant_id')} = current_setting('app.tenant_id', true)
        AND (
            current_setting('app.account_id', true) = ''
            OR {column('account_id')} = current_setting('app.account_id', true)
        )
        AND (
            current_setting('app.space_id', true) = ''
            OR {column('space_id')} = current_setting('app.space_id', true)
        )
    """


def _grant_database_connect(role: str) -> None:
    """Grant only CONNECT on the database the migration is actually targeting."""
    op.execute(
        f"""
        DO $$
        BEGIN
            EXECUTE format(
                'GRANT CONNECT ON DATABASE %I TO %I',
                current_database(),
                '{role}'
            );
        END
        $$
        """
    )


def upgrade() -> None:
    app_role = _configured_role(APP_ROLE_ENV)
    worker_role = _configured_role(WORKER_ROLE_ENV)
    if app_role == worker_role:
        raise RuntimeError(
            f"{APP_ROLE_ENV} and {WORKER_ROLE_ENV} must name different login roles."
        )
    _role_exists(app_role)
    _role_exists(worker_role)

    app_ident = _ident(app_role)
    worker_ident = _ident(worker_role)

    # The init script creates the two logins before Alembic.  Repeat the
    # database/schema grants here for non-box deployments and for an existing
    # database that is being adopted into the role split.  Application DML is
    # granted for every current non-queue table; RLS remains the row boundary.
    # The worker starts with no schema-table privileges at all and receives only
    # its narrow queue grants below.
    _grant_database_connect(app_role)
    _grant_database_connect(worker_role)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {app_ident}, {worker_ident}")
    # PostgreSQL versions that still give PUBLIC CREATE on public would otherwise
    # hand every login DDL authority despite the per-role revoke below.
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {app_ident}, {worker_ident}")
    op.execute(f"GRANT {_APP_DML_PRIVILEGES} ON ALL TABLES IN SCHEMA public TO {app_ident}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {app_ident}")
    # Default privileges apply to tables/sequences subsequently created by this
    # migration owner.  Future migrations must continue using this same owner;
    # a new owner must establish the equivalent defaults before it creates data.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT {_APP_DML_PRIVILEGES} ON TABLES TO {app_ident}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {app_ident}"
    )
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {worker_ident}")
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {worker_ident}")

    for table in JOB_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    for table, policies in {
        "jobs": (
            "onebrain_jobs_admin",
            "onebrain_jobs_app_select",
            "onebrain_jobs_app_insert",
            "onebrain_jobs_worker_select",
            "onebrain_jobs_worker_update",
        ),
        "job_files": (
            "onebrain_job_files_admin",
            "onebrain_job_files_app_insert",
            "onebrain_job_files_worker_select",
        ),
    }.items():
        for policy in policies:
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")

    # Owner/migration/operator roles retain access only through the existing
    # role-identity-bound admin predicate.  No user-controlled GUC can enable
    # this policy for an application or worker connection.
    op.execute(
        """
        CREATE POLICY onebrain_jobs_admin ON jobs
        AS PERMISSIVE FOR ALL TO PUBLIC
        USING (_onebrain_rls_admin())
        WITH CHECK (_onebrain_rls_admin())
        """
    )
    op.execute(
        f"""
        CREATE POLICY onebrain_jobs_app_select ON jobs
        AS PERMISSIVE FOR SELECT TO {app_ident}
        USING ({_scope_predicate()})
        """
    )
    op.execute(
        f"""
        CREATE POLICY onebrain_jobs_app_insert ON jobs
        AS PERMISSIVE FOR INSERT TO {app_ident}
        WITH CHECK ({_scope_predicate()})
        """
    )
    # The worker role is privileged only on the queue.  Its table privileges
    # below exclude INSERT, DELETE, and customer scope/payload columns.
    op.execute(
        f"""
        CREATE POLICY onebrain_jobs_worker_select ON jobs
        AS PERMISSIVE FOR SELECT TO {worker_ident}
        USING (true)
        """
    )
    op.execute(
        f"""
        CREATE POLICY onebrain_jobs_worker_update ON jobs
        AS PERMISSIVE FOR UPDATE TO {worker_ident}
        USING (true)
        WITH CHECK (true)
        """
    )

    op.execute(
        """
        CREATE POLICY onebrain_job_files_admin ON job_files
        AS PERMISSIVE FOR ALL TO PUBLIC
        USING (_onebrain_rls_admin())
        WITH CHECK (_onebrain_rls_admin())
        """
    )
    op.execute(
        f"""
        CREATE POLICY onebrain_job_files_app_insert ON job_files
        AS PERMISSIVE FOR INSERT TO {app_ident}
        WITH CHECK (
            EXISTS (
                SELECT 1
                FROM jobs j
                WHERE j.id = job_files.job_id
                  AND {_scope_predicate('j.')}
            )
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY onebrain_job_files_worker_select ON job_files
        AS PERMISSIVE FOR SELECT TO {worker_ident}
        USING (true)
        """
    )

    # Remove broad/default access on the queue after the application-wide DML
    # grant above.  The request role deliberately has no SELECT privilege on
    # jobs.payload or job_files, while the worker role can neither insert/delete
    # queue rows nor rewrite their tenant/payload fields.
    op.execute("REVOKE ALL PRIVILEGES ON TABLE jobs, job_files FROM PUBLIC")
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE jobs, job_files FROM {app_ident}, {worker_ident}")
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE alembic_version FROM {app_ident}")
    op.execute(f"GRANT SELECT ({_APP_JOB_SELECT_COLUMNS}) ON TABLE jobs TO {app_ident}")
    op.execute(f"GRANT INSERT ({_APP_JOB_INSERT_COLUMNS}) ON TABLE jobs TO {app_ident}")
    op.execute(f"GRANT INSERT ({_APP_JOB_FILE_INSERT_COLUMNS}) ON TABLE job_files TO {app_ident}")
    # Store constructors verify the migrated revision through this table before
    # touching customer data; a restricted app role needs only this read.
    op.execute(f"GRANT SELECT ON TABLE alembic_version TO {app_ident}")
    op.execute(f"GRANT SELECT ON TABLE jobs, job_files TO {worker_ident}")
    op.execute(f"GRANT UPDATE ({_WORKER_JOB_UPDATE_COLUMNS}) ON TABLE jobs TO {worker_ident}")


def downgrade() -> None:
    # Downgrades are intentionally conservative: revoke the least-privilege
    # grants and remove only policies this revision owns.  Do not disable RLS on
    # either table because a downgrade must not silently widen production data.
    app_role = _configured_role(APP_ROLE_ENV)
    worker_role = _configured_role(WORKER_ROLE_ENV)
    app_ident = _ident(app_role)
    worker_ident = _ident(worker_role)

    for table, policies in {
        "job_files": (
            "onebrain_job_files_worker_select",
            "onebrain_job_files_app_insert",
            "onebrain_job_files_admin",
        ),
        "jobs": (
            "onebrain_jobs_worker_update",
            "onebrain_jobs_worker_select",
            "onebrain_jobs_app_insert",
            "onebrain_jobs_app_select",
            "onebrain_jobs_admin",
        ),
    }.items():
        for policy in policies:
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")

    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE jobs, job_files FROM {app_ident}, {worker_ident}")
    op.execute(f"REVOKE SELECT ON TABLE alembic_version FROM {app_ident}")
    op.execute(f"REVOKE {_APP_DML_PRIVILEGES} ON ALL TABLES IN SCHEMA public FROM {app_ident}")
    op.execute(f"REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public FROM {app_ident}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE {_APP_DML_PRIVILEGES} ON TABLES FROM {app_ident}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE USAGE, SELECT ON SEQUENCES FROM {app_ident}"
    )
