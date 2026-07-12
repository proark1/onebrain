"""Classify a release's migrations as code_only vs restore_required
(architecture §3a). Fail-closed: anything unparseable is restore_required.

Ground rules from the comm repo (assaddar-ai-communication/packages/db):
migrations are raw .sql files with NO down migrations; the runner keys
schema_migrations on the FULL FILENAME and applies files in lexicographic
filename order; duplicate numeric prefixes are legal (two 0010_*.sql files are
distinct versions). So this module keys by filename, never by numeric prefix.

Alembic sources are classified over the upgrade() body only. Any op.execute
whose argument is not a plain string literal (a variable, f-string, or
concatenation — including the house 0017 loop/f-string style) is a blocking
`opaque_execute` finding: this is the deliberate fail-closed posture. The
operator overrides a false positive by supplying `rollback_kind` explicitly at
release creation — same escape hatch as the blocking SQL rules (B5).

Grandfathering is a CALLER contract: the classifier is applied only to the
migration files NEW in the release being promoted — the CLI/CI passes the
delta file set, never the whole history (comm's historical destructive files
stay untouched).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import List, Sequence, Tuple

ROLLBACK_CODE_ONLY = "code_only"
ROLLBACK_RESTORE_REQUIRED = "restore_required"

_EXCERPT_CHARS = 120


@dataclass(frozen=True)
class LintFinding:
    source: str        # filename or caller-supplied label
    rule: str          # rule id (see _BLOCKING_RULES / update_* / add_constraint*)
    blocking: bool     # True -> forces restore_required
    excerpt: str       # first 120 chars of the offending statement (SQL only, no data)


@dataclass(frozen=True)
class MigrationClassification:
    rollback_kind: str                     # code_only | restore_required
    findings: Tuple[LintFinding, ...]


# --- SQL normalization (applied before rule matching, in this order) ----------
# 1. line comments; 2. block comments; 3. dollar-quoted bodies (function bodies
# are opaque, so a DROP TABLE inside a plpgsql string never false-positives);
# 4. single-quoted literals (no data ever reaches a finding excerpt); 5. split
# statements on ';'.
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_DOLLAR_QUOTE_RE = re.compile(r"\$(?P<tag>[A-Za-z0-9_]*)\$.*?\$(?P=tag)\$", re.S)
_SINGLE_QUOTE_RE = re.compile(r"'(?:[^']|'')*'")


def _normalize_sql(sql_text: str) -> List[str]:
    text = _LINE_COMMENT_RE.sub(" ", sql_text or "")
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    text = _DOLLAR_QUOTE_RE.sub(" ", text)
    text = _SINGLE_QUOTE_RE.sub(" ", text)
    return [stmt.strip() for stmt in text.split(";") if stmt.strip()]


# --- rules (case-insensitive, per normalized statement) -----------------------
_BLOCKING_RULES: Tuple[Tuple[str, re.Pattern], ...] = (
    ("drop_table", re.compile(r"\bDROP\s+TABLE\b", re.I)),
    ("drop_column", re.compile(r"\bDROP\s+COLUMN\b", re.I)),
    ("drop_schema", re.compile(r"\bDROP\s+SCHEMA\b", re.I)),
    ("rename", re.compile(r"\bRENAME\s+(?:TO|COLUMN)\b", re.I)),
    ("retype", re.compile(r"\bALTER\s+COLUMN\s+\S+\s+(?:SET\s+DATA\s+)?TYPE\b", re.I)),
    ("set_not_null", re.compile(r"\bALTER\s+COLUMN\s+\S+\s+SET\s+NOT\s+NULL\b", re.I)),
    ("truncate", re.compile(r"\bTRUNCATE\b", re.I)),
    ("delete_rows", re.compile(r"\bDELETE\s+FROM\b", re.I)),
)
_ADD_COLUMN_RE = re.compile(r"\bADD\s+COLUMN\b", re.I)
_NOT_NULL_RE = re.compile(r"\bNOT\s+NULL\b", re.I)
_DEFAULT_RE = re.compile(r"\bDEFAULT\b", re.I)
_GENERATED_IDENTITY_RE = re.compile(r"\bGENERATED\b.*\bIDENTITY\b", re.I | re.S)
_UPDATE_DML_RE = re.compile(r"\bUPDATE\s+\S+\s+SET\b", re.I)
# The one non-blocking UPDATE form (B5): a pure backfill of a freshly-added
# nullable column — single SET column, and the SAME column null-guarded.
_UPDATE_BACKFILL_RE = re.compile(
    r"\bUPDATE\s+\S+\s+SET\s+(?P<col>[a-z0-9_.\"]+)\s*=\s*[^;]*\bWHERE\s+(?P=col)\s+IS\s+NULL\s*$",
    re.I,
)
_ADD_CONSTRAINT_RE = re.compile(r"\bADD\s+CONSTRAINT\b", re.I)
_NOT_VALID_RE = re.compile(r"\bNOT\s+VALID\b", re.I)


def _excerpt(stmt: str) -> str:
    return stmt[:_EXCERPT_CHARS]


def _classify_statement(stmt: str, source: str) -> List[LintFinding]:
    findings: List[LintFinding] = []
    for rule, pattern in _BLOCKING_RULES:
        if pattern.search(stmt):
            findings.append(LintFinding(source, rule, True, _excerpt(stmt)))
    if _ADD_COLUMN_RE.search(stmt) and _NOT_NULL_RE.search(stmt) \
            and not _DEFAULT_RE.search(stmt) and not _GENERATED_IDENTITY_RE.search(stmt):
        findings.append(LintFinding(source, "add_not_null_no_default", True, _excerpt(stmt)))
    if _UPDATE_DML_RE.search(stmt):
        if _UPDATE_BACKFILL_RE.search(stmt):
            findings.append(LintFinding(source, "update_backfill", False, _excerpt(stmt)))
        else:
            # B5: a mass UPDATE overwrites data the previous image can never
            # restore — as destructive as DELETE FROM.
            findings.append(LintFinding(source, "update_dml", True, _excerpt(stmt)))
    if _ADD_CONSTRAINT_RE.search(stmt):
        if _NOT_VALID_RE.search(stmt):
            # NOT VALID skips existing-row validation — the documented
            # expand-phase idiom stays informational.
            findings.append(LintFinding(source, "add_constraint_not_valid", False, _excerpt(stmt)))
        else:
            # B5: a validated CHECK/UNIQUE/FK can make the PREVIOUS image's
            # writes fail at runtime after a code_only revert.
            findings.append(LintFinding(source, "add_constraint", True, _excerpt(stmt)))
    return findings


def _classification(findings: Sequence[LintFinding]) -> MigrationClassification:
    kind = ROLLBACK_RESTORE_REQUIRED if any(f.blocking for f in findings) else ROLLBACK_CODE_ONLY
    return MigrationClassification(rollback_kind=kind, findings=tuple(findings))


def classify_sql(sql_text: str, source: str = "<sql>") -> MigrationClassification:
    findings: List[LintFinding] = []
    for stmt in _normalize_sql(sql_text):
        findings.extend(_classify_statement(stmt, source))
    return _classification(findings)


def classify_sql_files(files: Sequence[Tuple[str, str]]) -> MigrationClassification:
    """files = (filename, sql_text); evaluated in lexicographic filename order
    (== the comm runner's apply order); duplicate numeric prefixes are fine —
    findings are keyed by the FULL filename, never a numeric prefix."""
    findings: List[LintFinding] = []
    for filename, sql_text in sorted(files, key=lambda pair: pair[0]):
        findings.extend(classify_sql(sql_text, source=filename).findings)
    return _classification(findings)


# --- alembic upgrade() classification ------------------------------------------

_ALEMBIC_OP_RULES = {
    "drop_table": "drop_table",
    "drop_column": "drop_column",
    "rename_table": "rename",
}


def _call_excerpt(node: ast.Call, py_source: str) -> str:
    try:
        return _excerpt(ast.get_source_segment(py_source, node) or ast.dump(node))
    except Exception:
        return ""


def _is_op_call(node: ast.Call, attr: str) -> bool:
    func = node.func
    return (isinstance(func, ast.Attribute) and func.attr == attr
            and isinstance(func.value, ast.Name) and func.value.id == "op")


def classify_alembic_source(py_source: str, source: str = "<alembic>") -> MigrationClassification:
    """Only the upgrade() body counts — a drop in downgrade() must not flag.
    Fail-closed: an unparseable source, or one with no upgrade(), is a blocking
    `unparseable_source` finding (the operator override at release creation is
    the escape hatch, never a silent pass)."""
    try:
        tree = ast.parse(py_source)
    except SyntaxError:
        return _classification([LintFinding(source, "unparseable_source", True, "")])
    upgrade = next(
        (node for node in tree.body
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "upgrade"),
        None,
    )
    if upgrade is None:
        return _classification([LintFinding(source, "unparseable_source", True, "")])

    findings: List[LintFinding] = []
    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call):
            continue
        for attr, rule in _ALEMBIC_OP_RULES.items():
            if _is_op_call(node, attr):
                findings.append(LintFinding(source, rule, True, _call_excerpt(node, py_source)))
        if _is_op_call(node, "alter_column"):
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            if "type_" in keywords:
                findings.append(LintFinding(source, "retype", True, _call_excerpt(node, py_source)))
            nullable = keywords.get("nullable")
            if isinstance(nullable, ast.Constant) and nullable.value is False:
                findings.append(LintFinding(source, "set_not_null", True, _call_excerpt(node, py_source)))
        if _is_op_call(node, "execute"):
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                # Literal SQL (adjacent-literal concatenation is folded by the
                # parser) — classify it like a raw-SQL migration statement.
                findings.extend(classify_sql(arg.value, source=f"{source}:execute").findings)
            else:
                # Variable / f-string / concatenation — opaque, fail closed.
                # The house loop style (0017's `for col in ...: op.execute(f"...")`)
                # lands here by design.
                findings.append(LintFinding(source, "opaque_execute", True, _call_excerpt(node, py_source)))
    return _classification(findings)


def classify_release(*, alembic_sources: Sequence[Tuple[str, str]] = (),
                     sql_files: Sequence[Tuple[str, str]] = ()) -> MigrationClassification:
    """Merge alembic + raw-SQL classifications; restore_required iff ANY
    blocking finding. Callers pass only the files NEW in the release being
    promoted (grandfathering — see module docstring)."""
    findings: List[LintFinding] = []
    for name, py_source in alembic_sources:
        findings.extend(classify_alembic_source(py_source, source=name).findings)
    findings.extend(classify_sql_files(sql_files).findings)
    return _classification(findings)
