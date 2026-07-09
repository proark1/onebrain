"""Bind the RLS admin bypass to the privileged operator role, not a session flag.

0008 defined `_onebrain_rls_admin()` as `current_setting('app.onebrain_admin') =
'true'` — a transaction-local GUC that the *runtime* database role can set on its
own connection, so it could self-assert admin and read across every account
(proven: the app role expanded visible accounts from one to two).

This migration redefines the bypass to depend on the connected role's identity:
it is granted only to a superuser or a role that owns / is a member of the role
that owns the platform tables. The runtime role (NOBYPASSRLS, non-owner, not a
member) can never satisfy it, and there is no forgeable flag. Legitimate
cross-account operator reads now go through a connection authenticated as the
operator/owner role (ONEBRAIN_OPERATOR_DATABASE_URL, falling back to the
migration DSN) — see PgVectorStore/PostgresPlatformStore/etc.

The policies themselves are unchanged; they call `_onebrain_rls_admin()`, so
redefining the function updates every table at once. FORCE ROW LEVEL SECURITY
stays on.

Revision ID: 0009_rls_admin_role
Revises: 0008_rls_hardening
Create Date: 2026-07-10
"""

from __future__ import annotations

from alembic import op


revision = "0009_rls_admin_role"
down_revision = "0008_rls_hardening"
branch_labels = None
depends_on = None


# Non-forgeable: bypass is granted only to a superuser or the role that owns the
# platform tables (or a member of it). current_user is the connected role under
# SECURITY INVOKER, and the runtime role can neither become a superuser nor grant
# itself membership in the owner role.
_ROLE_ADMIN_FN = """
    CREATE OR REPLACE FUNCTION _onebrain_rls_admin()
    RETURNS boolean
    LANGUAGE sql
    STABLE
    AS $$
        SELECT current_setting('is_superuser', true) = 'on'
            OR pg_has_role(
                   current_user,
                   (SELECT tableowner FROM pg_tables WHERE tablename = 'platform_accounts' LIMIT 1),
                   'MEMBER'
               )
    $$
"""

# The original, forgeable GUC-based definition (restored on downgrade only).
_GUC_ADMIN_FN = """
    CREATE OR REPLACE FUNCTION _onebrain_rls_admin()
    RETURNS boolean
    LANGUAGE sql
    STABLE
    AS $$
        SELECT current_setting('app.onebrain_admin', true) = 'true'
    $$
"""


def upgrade() -> None:
    op.execute(_ROLE_ADMIN_FN)


def downgrade() -> None:
    op.execute(_GUC_ADMIN_FN)
