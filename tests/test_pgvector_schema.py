from __future__ import annotations

import pytest

from app.store.pgvector import PgVectorStore


class FakeCursor:
    def __init__(self, existing_dim: int):
        self.existing_dim = existing_dim
        self.sql: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchone(self):
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
    store._conn = lambda: FakeConnection(cursor)

    with pytest.raises(RuntimeError, match="embedding dimension"):
        store._init_schema()

    assert not any("DROP TABLE" in sql.upper() for sql in cursor.sql)
