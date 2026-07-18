"""Add Drive identity foundations and close terminal job byte retention.

Revision ID: 0032_drive_foundations
Revises: 0031_mc_user_management
Create Date: 2026-07-18
"""

from __future__ import annotations

import os
import re

from alembic import op


revision = "0032_drive_foundations"
down_revision = "0031_mc_user_management"
branch_labels = None
depends_on = None


APP_ROLE_ENV = "ONEBRAIN_POSTGRES_APP_ROLE"
WORKER_ROLE_ENV = "ONEBRAIN_POSTGRES_WORKER_ROLE"
_ROLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
ACCESS_TABLES = ("platform_access_groups", "platform_access_group_memberships")


def _role(env_name: str) -> str:
    value = os.environ.get(env_name, "").strip()
    if not _ROLE_NAME.fullmatch(value):
        raise RuntimeError(f"{env_name} must name a simple PostgreSQL login role.")
    return value


def _ident(value: str) -> str:
    return f'"{value}"'


def _scope(prefix: str = "") -> str:
    return f"""
        {prefix}tenant_id = current_setting('app.tenant_id', true)
        AND (current_setting('app.account_id', true) = ''
             OR {prefix}account_id = current_setting('app.account_id', true))
        AND (current_setting('app.space_id', true) = ''
             OR COALESCE({prefix}space_id, '') = current_setting('app.space_id', true))
    """


def upgrade() -> None:
    app_role = _role(APP_ROLE_ENV)
    worker_role = _role(WORKER_ROLE_ENV)
    app_ident = _ident(app_role)
    worker_ident = _ident(worker_role)

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_access_groups (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            space_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'department' CHECK (kind IN ('department', 'team')),
            name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT platform_access_groups_scope_unique UNIQUE (id, tenant_id, account_id),
            CONSTRAINT platform_access_groups_name_unique UNIQUE (account_id, space_id, kind, name)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_access_group_memberships (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES platform_accounts(id) ON DELETE CASCADE,
            space_id TEXT NOT NULL DEFAULT '',
            group_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT platform_access_group_membership_unique UNIQUE (account_id, group_id, user_id),
            CONSTRAINT platform_access_group_membership_group_fk
                FOREIGN KEY (group_id, tenant_id, account_id)
                REFERENCES platform_access_groups(id, tenant_id, account_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS platform_access_group_memberships_user_idx "
        "ON platform_access_group_memberships (tenant_id, account_id, user_id, status)"
    )

    for table in ACCESS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS onebrain_{table}_scope ON {table}")
        op.execute(
            f"""
            CREATE POLICY onebrain_{table}_scope ON {table}
            USING (_onebrain_rls_admin() OR ({_scope()}))
            WITH CHECK (_onebrain_rls_admin() OR ({_scope()}))
            """
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {app_ident}")

    # Privacy erasure can delete only jobs in its current RLS scope. Raw job-file
    # bytes remain unreadable to the request role and disappear by FK cascade.
    op.execute("DROP POLICY IF EXISTS onebrain_jobs_app_delete ON jobs")
    op.execute(
        f"CREATE POLICY onebrain_jobs_app_delete ON jobs AS PERMISSIVE FOR DELETE TO {app_ident} USING ({_scope()})"
    )
    op.execute(f"GRANT DELETE ON TABLE jobs TO {app_ident}")
    op.execute("DROP POLICY IF EXISTS onebrain_job_files_app_delete ON job_files")
    op.execute(
        f"""
        CREATE POLICY onebrain_job_files_app_delete ON job_files
        AS PERMISSIVE FOR DELETE TO {app_ident}
        USING (EXISTS (
            SELECT 1 FROM jobs j
            WHERE j.id = job_files.job_id AND ({_scope('j.')})
        ))
        """
    )
    op.execute(f"GRANT DELETE ON TABLE job_files TO {app_ident}")

    # A queue worker may delete a file payload only after its parent job is
    # terminal. It still has no INSERT/DELETE authority over the job row itself.
    op.execute("DROP POLICY IF EXISTS onebrain_job_files_worker_delete ON job_files")
    op.execute(
        f"""
        CREATE POLICY onebrain_job_files_worker_delete ON job_files
        AS PERMISSIVE FOR DELETE TO {worker_ident}
        USING (EXISTS (
            SELECT 1 FROM jobs j
            WHERE j.id = job_files.job_id AND j.status IN ('succeeded', 'failed')
        ))
        """
    )
    op.execute(f"GRANT DELETE ON TABLE job_files TO {worker_ident}")

    # Restore-required data minimization sweep: preserve job history rows and all
    # retryable/running bytes, remove only bytes whose parent is already terminal.
    op.execute(
        "DELETE FROM job_files jf USING jobs j "
        "WHERE jf.job_id = j.id AND j.status IN ('succeeded', 'failed')"
    )


def downgrade() -> None:
    app_role = _role(APP_ROLE_ENV)
    worker_role = _role(WORKER_ROLE_ENV)
    app_ident = _ident(app_role)
    worker_ident = _ident(worker_role)
    op.execute("DROP POLICY IF EXISTS onebrain_job_files_worker_delete ON job_files")
    op.execute("DROP POLICY IF EXISTS onebrain_job_files_app_delete ON job_files")
    op.execute("DROP POLICY IF EXISTS onebrain_jobs_app_delete ON jobs")
    op.execute(f"REVOKE DELETE ON TABLE job_files FROM {worker_ident}")
    op.execute(f"REVOKE DELETE ON TABLE job_files FROM {app_ident}")
    op.execute(f"REVOKE DELETE ON TABLE jobs FROM {app_ident}")
    for table in reversed(ACCESS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS onebrain_{table}_scope ON {table}")
        op.execute(f"DROP TABLE IF EXISTS {table}")
