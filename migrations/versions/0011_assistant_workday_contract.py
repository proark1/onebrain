"""Assistant contract catch-up: grant assistant_workday to full-contract grants.

Phase 4 of the assistant added the `assistant_workday` purpose (workday briefs,
inbox triage, follow-up risks, calendar insights, provider source records). The
contract vocabulary in app/assistant/contracts.py now includes it, and future
provisioning derives from that constant — but installations and service keys
provisioned earlier froze the pre-Phase-4 purpose list, so assistant writes with
purpose=assistant_workday fail scope checks.

This backfills `assistant_workday` ONLY into rows that were clearly minted as
"the full assistant contract of their era": active, pinned to app_id='assistant',
and holding exactly the old 14-purpose set. Deliberately narrowed keys or
installations keep their narrow grants.

Revision ID: 0011_assistant_workday_contract
Revises: 0010_append_only_audit
Create Date: 2026-07-10
"""

from __future__ import annotations

from alembic import op


revision = "0011_assistant_workday_contract"
down_revision = "0010_append_only_audit"
branch_labels = None
depends_on = None

# The full assistant.v1 purpose set as it existed before Phase 4 (sorted).
_OLD_FULL_PURPOSES = (
    "assistant_action",
    "assistant_briefing",
    "assistant_calendar_planning",
    "assistant_connected_account",
    "assistant_context",
    "assistant_feedback",
    "assistant_followup",
    "assistant_model_usage",
    "assistant_notification",
    "assistant_provider_health",
    "assistant_security",
    "assistant_settings",
    "assistant_sync",
    "assistant_voice",
)

_OLD_SET_SQL = "ARRAY[" + ", ".join(f"'{p}'" for p in _OLD_FULL_PURPOSES) + "]::text[]"

_NEW_PURPOSE = "assistant_workday"


def _add_purpose_sql(table: str, column: str) -> str:
    return f"""
        UPDATE {table}
        SET {column} = {column} || ',{_NEW_PURPOSE}'
        WHERE app_id = 'assistant'
          AND status = 'active'
          AND NOT ('{_NEW_PURPOSE}' = ANY(string_to_array({column}, ',')))
          AND (
            SELECT array_agg(p ORDER BY p)
            FROM unnest(string_to_array({column}, ',')) AS p
          ) = {_OLD_SET_SQL}
    """


def _remove_purpose_sql(table: str, column: str) -> str:
    return f"""
        UPDATE {table}
        SET {column} = array_to_string(
            array_remove(string_to_array({column}, ','), '{_NEW_PURPOSE}'), ','
        )
        WHERE app_id = 'assistant'
          AND '{_NEW_PURPOSE}' = ANY(string_to_array({column}, ','))
    """


# platform_app_installations enforces FORCE ROW LEVEL SECURITY (0008/0009). Under the
# app role the backfill UPDATE would silently match zero rows, so refuse to run unless
# the connection passes the repo's own RLS admin check (owner / migration DSN).
_REQUIRE_RLS_ADMIN = """
    DO $$
    BEGIN
        IF NOT _onebrain_rls_admin() THEN
            RAISE EXCEPTION
                'Migration 0011 must run with the owner/migration DSN: '
                'row-level security on platform_app_installations would silently '
                'skip the data backfill under the app role.';
        END IF;
    END;
    $$
"""


def upgrade() -> None:
    op.execute(_REQUIRE_RLS_ADMIN)
    op.execute(_add_purpose_sql("platform_app_installations", "allowed_purposes"))
    op.execute(_add_purpose_sql("service_keys", "purposes"))


def downgrade() -> None:
    op.execute(_REQUIRE_RLS_ADMIN)
    op.execute(_remove_purpose_sql("service_keys", "purposes"))
    op.execute(_remove_purpose_sql("platform_app_installations", "allowed_purposes"))
