from __future__ import annotations

import pytest

from app.db.schema import REQUIRED_ALEMBIC_REVISION
from app.store.pgvector import PgVectorStore


class FakeCursor:
    def __init__(self, existing_dim: int, version: str = REQUIRED_ALEMBIC_REVISION):
        self.existing_dim = existing_dim
        self.version = version
        self.sql: list[str] = []
        self.params: list[object] = []
        self.last_sql = ""
        self.last_params = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.last_params = params
        self.sql.append(sql)
        self.params.append(params)

    def fetchone(self):
        if "alembic_version" in self.last_sql:
            return (self.version,)
        if "to_regclass" in self.last_sql:
            return (self.last_params[0],)
        if "pg_attribute" in self.last_sql:
            return (self.existing_dim,)
        return (self.existing_dim,)


class FakeConnection:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        pass


def test_pgvector_dimension_mismatch_refuses_to_drop_chunks_table():
    cursor = FakeCursor(existing_dim=128)
    store = object.__new__(PgVectorStore)
    store._dim = 256
    store._raw_conn = lambda: FakeConnection(cursor)

    with pytest.raises(RuntimeError, match="embedding dimension"):
        store._validate_schema()

    assert not any(keyword in sql.upper() for sql in cursor.sql for keyword in ("CREATE ", "ALTER ", "DROP "))
