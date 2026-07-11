"""Revocable login sessions.

OneBrain's session cookie used to be a stateless, signed HMAC token: authentic
and self-expiring, but impossible to revoke before its multi-day TTL lapsed. That
made the offboarding requirement ("existing sessions are revoked", "refresh tokens
are invalidated") unimplementable — a fired employee's cookie kept working.

This adds a server-side session row per login. resolve_principal now requires the
row to be present and un-revoked, so logout, offboarding, and a force-revoke API
take effect on the very next request. The token still carries the signature and
expiry; the row adds revocation.

The table is looked up by an unguessable session id DURING authentication, before
any tenant context is established, so it is intentionally not placed under the
tenant row-level-security policies (0008/0009). It stores no business content —
only a session id, its user and tenant, and lifecycle timestamps.

Revision ID: 0012_auth_sessions
Revises: 0011_assistant_workday_contract
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op


revision = "0012_auth_sessions"
down_revision = "0011_assistant_workday_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_sessions (
            id          text PRIMARY KEY,
            user_id     text NOT NULL,
            tenant_id   text NOT NULL DEFAULT '',
            created_at  timestamptz NOT NULL DEFAULT now(),
            expires_at  timestamptz NOT NULL,
            revoked_at  timestamptz
        )
        """
    )
    # Fast "is this session live?" lookups and "revoke everything for this user".
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_sessions_active "
        "ON auth_sessions (id) WHERE revoked_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_sessions_user "
        "ON auth_sessions (user_id) WHERE revoked_at IS NULL"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry ON auth_sessions (expires_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auth_sessions")
