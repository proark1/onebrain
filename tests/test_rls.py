from __future__ import annotations

import pytest

from app.db.rls import PostgresRLSError, validate_rls_enabled


class FakeCursor:
    def __init__(self, enabled):
        self.enabled = enabled
        self.last_table = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _sql, params=None):
        self.last_table = params[0]

    def fetchone(self):
        return (self.enabled.get(self.last_table, False),)


class FakeConnection:
    def __init__(self, enabled):
        self.enabled = enabled

    def cursor(self):
        return FakeCursor(self.enabled)


def test_validate_rls_enabled_accepts_all_required_tables():
    validate_rls_enabled(FakeConnection({"chunks": True, "intake_records": True}), tables=("chunks", "intake_records"))


def test_validate_rls_enabled_reports_missing_tables():
    with pytest.raises(PostgresRLSError, match="intake_records"):
        validate_rls_enabled(FakeConnection({"chunks": True}), tables=("chunks", "intake_records"))
