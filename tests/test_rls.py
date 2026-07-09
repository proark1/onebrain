from __future__ import annotations

import pytest

from app.db.rls import PostgresRLSError, validate_rls_enabled


class FakeCursor:
    def __init__(self, states):
        self.states = states
        self.last_table = ""
        self.set_configs = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _sql, params=None):
        if params and len(params) == 1:
            self.last_table = params[0]
        else:
            self.set_configs.append(params)

    def fetchone(self):
        state = self.states.get(self.last_table, (False, False))
        if isinstance(state, bool):
            return (state, state)
        return state


class FakeConnection:
    def __init__(self, states):
        self.states = states
        self.cursor_obj = FakeCursor(states)

    def cursor(self):
        return self.cursor_obj


def test_validate_rls_enabled_accepts_all_required_tables():
    validate_rls_enabled(FakeConnection({"chunks": (True, True), "intake_records": (True, True)}), tables=("chunks", "intake_records"))


def test_validate_rls_enabled_reports_missing_tables():
    with pytest.raises(PostgresRLSError, match="intake_records"):
        validate_rls_enabled(FakeConnection({"chunks": (True, True)}), tables=("chunks", "intake_records"))


def test_validate_rls_enabled_requires_force_rls():
    with pytest.raises(PostgresRLSError, match="chunks"):
        validate_rls_enabled(FakeConnection({"chunks": (True, False)}), tables=("chunks",))
