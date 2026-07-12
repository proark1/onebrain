"""Promotion-time migration classifier (WP3): raw-SQL and alembic-upgrade rules
that stamp rollback_kind, fail-closed. Pure — no database, no alembic runtime."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.controlplane.migration_lint import (
    ROLLBACK_CODE_ONLY,
    ROLLBACK_RESTORE_REQUIRED,
    classify_alembic_source,
    classify_release,
    classify_sql,
    classify_sql_files,
)


# --- one (parametrized) test per blocking SQL rule -----------------------------

@pytest.mark.parametrize("sql,rule", [
    ("DROP TABLE users;", "drop_table"),
    ("ALTER TABLE users DROP COLUMN email;", "drop_column"),
    ("DROP SCHEMA legacy CASCADE;", "drop_schema"),
    ("ALTER TABLE users RENAME TO members;", "rename"),
    ("ALTER TABLE users RENAME COLUMN email TO mail;", "rename"),
    ("ALTER TABLE users ALTER COLUMN id TYPE bigint;", "retype"),
    ("ALTER TABLE users ALTER COLUMN id SET DATA TYPE bigint;", "retype"),
    ("ALTER TABLE users ALTER COLUMN email SET NOT NULL;", "set_not_null"),
    ("ALTER TABLE users ADD COLUMN tier text NOT NULL;", "add_not_null_no_default"),
    ("TRUNCATE audit_log;", "truncate"),
    ("DELETE FROM sessions WHERE expired;", "delete_rows"),
])
def test_blocking_rule(sql, rule):
    result = classify_sql(sql, source="0099_test.sql")
    assert result.rollback_kind == ROLLBACK_RESTORE_REQUIRED
    assert [f.rule for f in result.findings] == [rule]
    assert result.findings[0].blocking is True
    assert result.findings[0].source == "0099_test.sql"


def test_additive_only_is_code_only():
    sql = """
    CREATE TABLE widgets (id BIGSERIAL PRIMARY KEY, name TEXT);
    ALTER TABLE users ADD COLUMN tier TEXT NOT NULL DEFAULT 'free';
    ALTER TABLE users ADD COLUMN note TEXT;
    CREATE INDEX IF NOT EXISTS idx_users_tier ON users (tier);
    DROP INDEX IF EXISTS idx_users_old;
    """
    result = classify_sql(sql)
    assert result.rollback_kind == ROLLBACK_CODE_ONLY
    assert not any(f.blocking for f in result.findings)


def test_update_backfill_is_informational():
    # The narrow null-guarded form (B5): single SET column, same column guarded.
    result = classify_sql("UPDATE t SET x = 1 WHERE x IS NULL;")
    assert result.rollback_kind == ROLLBACK_CODE_ONLY
    assert [f.rule for f in result.findings] == ["update_backfill"]
    assert result.findings[0].blocking is False


def test_lossy_update_is_blocking():
    # B5: a mass UPDATE is as destructive as DELETE FROM — reverting the image
    # does not restore overwritten data.
    nulled = classify_sql("UPDATE users SET email = NULL;")
    assert nulled.rollback_kind == ROLLBACK_RESTORE_REQUIRED
    assert [f.rule for f in nulled.findings] == ["update_dml"]

    # Guard column != SET column is NOT the backfill form.
    mismatched = classify_sql("UPDATE t SET a = 1 WHERE b IS NULL;")
    assert mismatched.rollback_kind == ROLLBACK_RESTORE_REQUIRED
    assert [f.rule for f in mismatched.findings] == ["update_dml"]


def test_add_check_constraint_is_blocking():
    # B5: a validated constraint can make the PREVIOUS image's writes fail at
    # runtime after a code_only revert.
    validated = classify_sql("ALTER TABLE t ADD CONSTRAINT c CHECK (x > 0);")
    assert validated.rollback_kind == ROLLBACK_RESTORE_REQUIRED
    assert [f.rule for f in validated.findings] == ["add_constraint"]

    not_valid = classify_sql("ALTER TABLE t ADD CONSTRAINT c CHECK (x > 0) NOT VALID;")
    assert not_valid.rollback_kind == ROLLBACK_CODE_ONLY
    assert [f.rule for f in not_valid.findings] == ["add_constraint_not_valid"]
    assert not_valid.findings[0].blocking is False


def test_comments_and_strings_do_not_flag():
    sql = """
    -- DROP TABLE users
    /* DELETE FROM sessions; */
    INSERT INTO notes (body) VALUES ('DROP TABLE x');
    """
    result = classify_sql(sql)
    assert result.rollback_kind == ROLLBACK_CODE_ONLY
    assert result.findings == ()


def test_dollar_quoted_function_body_is_opaque():
    sql = """
    CREATE FUNCTION prune() RETURNS trigger AS $$
    BEGIN
        DELETE FROM t WHERE stale;
    END;
    $$ LANGUAGE plpgsql;
    """
    result = classify_sql(sql)
    assert result.rollback_kind == ROLLBACK_CODE_ONLY
    assert result.findings == ()


def test_multi_statement_file_reports_statement_rule():
    sql = """
    CREATE TABLE widgets (id BIGSERIAL PRIMARY KEY);
    ALTER TABLE users DROP COLUMN email;
    CREATE INDEX IF NOT EXISTS idx_w ON widgets (id);
    """
    result = classify_sql(sql, source="0042_mixed.sql")
    assert result.rollback_kind == ROLLBACK_RESTORE_REQUIRED
    assert len(result.findings) == 1
    assert result.findings[0].rule == "drop_column"
    assert "DROP COLUMN" in result.findings[0].excerpt


def test_duplicate_numeric_prefixes_both_classified():
    additive = "CREATE INDEX IF NOT EXISTS idx_admin_perf ON messages (created_at);"
    destructive = "ALTER TABLE messages DROP COLUMN legacy_flags;"
    # Input deliberately NOT in lexicographic order: the classifier must key and
    # order by the FULL FILENAME (the comm runner's apply order) — duplicate
    # numeric prefixes are two distinct versions, never collapsed.
    result = classify_sql_files([
        ("0010_admin_search_and_inbox_indexes.sql", destructive),
        ("0010_admin_performance_indexes.sql", additive),
    ])
    assert result.rollback_kind == ROLLBACK_RESTORE_REQUIRED
    assert [f.source for f in result.findings] == ["0010_admin_search_and_inbox_indexes.sql"]
    assert result.findings[0].rule == "drop_column"

    # Lexicographic order is observable in findings order when both duplicate-
    # prefix files carry findings and arrive reversed.
    both = classify_sql_files([
        ("0010_admin_search_and_inbox_indexes.sql", destructive),
        ("0010_admin_performance_indexes.sql", "TRUNCATE audit_log;"),
    ])
    assert [f.source for f in both.findings] == [
        "0010_admin_performance_indexes.sql",
        "0010_admin_search_and_inbox_indexes.sql",
    ]
    assert [f.rule for f in both.findings] == ["truncate", "drop_column"]


# --- alembic upgrade() rules ----------------------------------------------------

def test_alembic_drop_column_flags():
    source = """
from alembic import op

def upgrade() -> None:
    op.drop_column("users", "email")

def downgrade() -> None:
    pass
"""
    result = classify_alembic_source(source, source="0020_drop.py")
    assert result.rollback_kind == ROLLBACK_RESTORE_REQUIRED
    assert [f.rule for f in result.findings] == ["drop_column"]


def test_alembic_downgrade_only_drop_does_not_flag():
    source = """
from alembic import op

def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS tier TEXT NOT NULL DEFAULT ''")

def downgrade() -> None:
    op.drop_column("users", "tier")
    op.execute("DROP TABLE shadow")
"""
    result = classify_alembic_source(source)
    assert result.rollback_kind == ROLLBACK_CODE_ONLY
    assert result.findings == ()


def test_alembic_literal_execute_is_classified():
    source = """
from alembic import op

def upgrade() -> None:
    op.execute("ALTER TABLE x DROP COLUMN y")
"""
    result = classify_alembic_source(source, source="0021_exec.py")
    assert result.rollback_kind == ROLLBACK_RESTORE_REQUIRED
    assert [(f.source, f.rule) for f in result.findings] == [("0021_exec.py:execute", "drop_column")]


def test_alembic_fstring_execute_fails_closed():
    # The house 0017 loop/f-string style is opaque by design (fail-closed); the
    # operator override at release creation is the escape hatch.
    source = """
from alembic import op

_COLUMNS = ("a", "b")

def upgrade() -> None:
    for col in _COLUMNS:
        op.execute(f"ALTER TABLE t ADD COLUMN IF NOT EXISTS {col} TEXT NOT NULL DEFAULT ''")
"""
    result = classify_alembic_source(source)
    assert result.rollback_kind == ROLLBACK_RESTORE_REQUIRED
    assert [f.rule for f in result.findings] == ["opaque_execute"]


def test_alembic_alter_column_nullable_false_flags():
    source = """
from alembic import op
import sqlalchemy as sa

def upgrade() -> None:
    op.alter_column("users", "email", nullable=False)
"""
    result = classify_alembic_source(source)
    assert result.rollback_kind == ROLLBACK_RESTORE_REQUIRED
    assert [f.rule for f in result.findings] == ["set_not_null"]

    retyped = classify_alembic_source("""
from alembic import op
import sqlalchemy as sa

def upgrade() -> None:
    op.alter_column("users", "id", type_=sa.BigInteger())
""")
    assert retyped.rollback_kind == ROLLBACK_RESTORE_REQUIRED
    assert [f.rule for f in retyped.findings] == ["retype"]


def test_alembic_unparseable_source_fails_closed():
    broken = classify_alembic_source("def upgrade(:\n    pass\n", source="0022_broken.py")
    assert broken.rollback_kind == ROLLBACK_RESTORE_REQUIRED
    assert [f.rule for f in broken.findings] == ["unparseable_source"]

    # Parseable but with no upgrade() — a malformed migration must not pass.
    no_upgrade = classify_alembic_source("x = 1\n")
    assert no_upgrade.rollback_kind == ROLLBACK_RESTORE_REQUIRED
    assert [f.rule for f in no_upgrade.findings] == ["unparseable_source"]


def test_classify_release_merges_lanes():
    result = classify_release(
        alembic_sources=[("0020_add.py", """
from alembic import op

def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS idx ON t (a)")
""")],
        sql_files=[("0011_cleanup.sql", "DELETE FROM sessions;")],
    )
    assert result.rollback_kind == ROLLBACK_RESTORE_REQUIRED
    assert [(f.source, f.rule) for f in result.findings] == [("0011_cleanup.sql", "delete_rows")]

    clean = classify_release(sql_files=[("0012_add.sql", "CREATE TABLE t (id INT);")])
    assert clean.rollback_kind == ROLLBACK_CODE_ONLY


def test_excerpt_contains_sql_only_no_literals():
    result = classify_sql("DELETE FROM notes WHERE body = 'customer secret';")
    assert result.findings[0].rule == "delete_rows"
    assert "customer secret" not in result.findings[0].excerpt


# --- CLI wiring (scripts/sign_release.py classify) -------------------------------

def _cli_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "sign_release.py"
    spec = importlib.util.spec_from_file_location("sign_release_cli_classify", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_classify_cli_exit_codes_and_json(tmp_path, capsys):
    cli = _cli_module()
    sql_dir = tmp_path / "migrations"
    sql_dir.mkdir()
    (sql_dir / "0010_add.sql").write_text("CREATE TABLE t (id INT);", encoding="utf-8")
    (sql_dir / "0011_drop.sql").write_text("ALTER TABLE t DROP COLUMN id;", encoding="utf-8")
    alembic_file = tmp_path / "0020_ok.py"
    alembic_file.write_text(
        "from alembic import op\n\ndef upgrade() -> None:\n"
        "    op.execute(\"CREATE INDEX IF NOT EXISTS i ON t (id)\")\n",
        encoding="utf-8",
    )

    exit_code = cli.main([
        "classify", "--sql-dir", str(sql_dir), "--alembic-file", str(alembic_file)])
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert out["rollback_kind"] == "restore_required"
    assert [(f["source"], f["rule"]) for f in out["findings"]] == [("0011_drop.sql", "drop_column")]

    exit_code = cli.main(["classify", "--sql-file", str(sql_dir / "0010_add.sql")])
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert out["rollback_kind"] == "code_only"
    assert out["findings"] == []
