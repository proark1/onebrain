"""Classify a release's migrations as code_only vs restore_required
(architecture §3a). Fail-closed: anything unparseable is restore_required.

Ground rules from the comm repo (assaddar-ai-communication/packages/db):
migrations are raw .sql files with NO down migrations; the runner keys
schema_migrations on the FULL FILENAME and applies files in lexicographic
filename order; duplicate numeric prefixes are legal (two 0010_*.sql files are
distinct versions). So this module keys by filename, never by numeric prefix.

Alembic sources are classified over the upgrade() body only, and EVERY call
reachable from it must be positively recognized (fail-closed):
- op.drop_table / op.drop_column / op.rename_table — blocking rules; the same
  attribute names on ANY other receiver (the op.batch_alter_table idiom) are
  blocking too.
- op.alter_column — blocking when it retypes (type_=), sets NOT NULL
  (nullable=False), or renames (new_column_name=).
- op.execute with a plain string literal — the literal is classified like a
  raw-SQL migration statement. Any other argument (variable, f-string,
  concatenation — including the house 0017 loop/f-string style) is a blocking
  `opaque_execute` finding.
- Purely-computational calls are allowlisted: additive op helpers
  (create_table/add_column/create_index/drop_index/...), sa./sqlalchemy.
  constructors, a fixed set of builtins and str/list/dict-style methods, and
  in-file helper functions — helper bodies are walked with these same rules
  (helpers passed as arguments to another call are walked too).
- EVERYTHING else — .execute() on any receiver (op.get_bind()/conn/session),
  op.get_bind() itself, op.batch_alter_table(), constraint-creating op
  helpers, and calls to functions defined outside the migration file — is a
  blocking `non_op_execution` finding: this module cannot prove such a call
  does not execute SQL.
The operator overrides a false positive by supplying `rollback_kind` explicitly
at release creation — same escape hatch as the blocking SQL rules (B5).

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
# Postgres makes the COLUMN keyword OPTIONAL in every ALTER TABLE column action
# ("ALTER TABLE t DROP c" == "... DROP COLUMN c"), so each column rule matches
# both spellings. The keyword-optional forms are scoped/guarded so the safe
# statements stay silent: standalone DROP INDEX never matches drop_column, and
# DROP CONSTRAINT / DROP DEFAULT / DROP NOT NULL / DROP IDENTITY /
# DROP EXPRESSION are per-column attribute drops that lose no row data.
_BLOCKING_RULES: Tuple[Tuple[str, re.Pattern], ...] = (
    ("drop_table", re.compile(r"\bDROP\s+TABLE\b", re.I)),
    ("drop_column", re.compile(
        r"\bALTER\s+TABLE\b[^;]*?\bDROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?"
        r"(?!CONSTRAINT\b|DEFAULT\b|NOT\b|IDENTITY\b|EXPRESSION\b)[A-Za-z0-9_\"]",
        re.I)),
    ("drop_schema", re.compile(r"\bDROP\s+SCHEMA\b", re.I)),
    # RENAME TO (table), RENAME [COLUMN] a TO b. RENAME CONSTRAINT a TO b never
    # matches (the ident branch requires whitespace-TO right after the first
    # word) — a constraint rename is metadata-only.
    ("rename", re.compile(r"\bRENAME\s+(?:TO\b|COLUMN\b|[A-Za-z0-9_\"]+\s+TO\b)", re.I)),
    ("retype", re.compile(
        r"\bALTER\s+(?:COLUMN\s+)?(?!TABLE\b)\S+\s+(?:SET\s+DATA\s+)?TYPE\b", re.I)),
    ("set_not_null", re.compile(
        r"\bALTER\s+(?:COLUMN\s+)?(?!TABLE\b)\S+\s+SET\s+NOT\s+NULL\b", re.I)),
    ("truncate", re.compile(r"\bTRUNCATE\b", re.I)),
    ("delete_rows", re.compile(r"\bDELETE\s+FROM\b", re.I)),
)
# ADD [COLUMN] <name>: constraint-introducing ADDs (CONSTRAINT and the unnamed
# table-constraint forms) are add_constraint territory, not a column add.
_ADD_COLUMN_RE = re.compile(
    r"\bADD\s+(?:COLUMN\s+)?"
    r"(?!CONSTRAINT\b|PRIMARY\b|UNIQUE\b|CHECK\b|FOREIGN\b|EXCLUDE\b)[A-Za-z0-9_\"]",
    re.I)
_NOT_NULL_RE = re.compile(r"\bNOT\s+NULL\b", re.I)
_DEFAULT_RE = re.compile(r"\bDEFAULT\b", re.I)
_GENERATED_IDENTITY_RE = re.compile(r"\bGENERATED\b.*\bIDENTITY\b", re.I | re.S)
_UPDATE_DML_RE = re.compile(r"\bUPDATE\s+\S+\s+SET\b", re.I)
# The one non-blocking UPDATE form (B5): a pure backfill of a freshly-added
# nullable column — a SINGLE SET assignment, and the SAME column null-guarded.
# [^,;]* forbids any comma between the assignment and WHERE, so a second
# assignment ("SET a = 1, b = NULL WHERE a IS NULL") can never ride the
# carve-out. Cost: a value expression containing a comma (COALESCE(a, b))
# falls to blocking update_dml — fail-closed, operator override available.
_UPDATE_BACKFILL_RE = re.compile(
    r"\bUPDATE\s+\S+\s+SET\s+(?P<col>[a-z0-9_.\"]+)\s*=\s*[^,;]*\bWHERE\s+(?P=col)\s+IS\s+NULL\s*$",
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

# Fail-closed call walk: every Call reachable from upgrade() must be POSITIVELY
# recognized — classified by a rule, or named on one of these purely-
# computational allowlists. Anything else is a blocking `non_op_execution`
# finding: this module cannot prove an unrecognized call does not execute SQL
# (conn/session.execute, op.get_bind(), op.batch_alter_table(), imported
# helpers). map/filter are deliberately absent — they exist to call things.
_SAFE_OP_CALLS = frozenset({
    # additive-only alembic ops (the SQL-lane statements that yield no finding)
    "create_table", "add_column", "create_index", "drop_index", "bulk_insert",
    "create_table_comment", "drop_table_comment",
    # computational helpers on op
    "f", "inline_literal",
})
_SAFE_CALL_ROOTS = frozenset({"sa", "sqlalchemy", "postgresql", "datetime", "uuid", "json"})
_SAFE_BUILTIN_CALLS = frozenset({
    "str", "int", "float", "bool", "bytes", "len", "list", "dict", "set",
    "tuple", "frozenset", "sorted", "reversed", "enumerate", "range", "zip",
    "min", "max", "sum", "abs", "round", "repr", "format", "print",
    "isinstance", "any", "all",
})
_SAFE_METHOD_CALLS = frozenset({
    # str/list/dict/datetime-style computational methods (receiver-agnostic)
    "join", "format", "format_map", "strip", "lstrip", "rstrip", "upper",
    "lower", "title", "replace", "split", "rsplit", "splitlines",
    "startswith", "endswith", "removeprefix", "removesuffix", "encode",
    "decode", "zfill", "append", "extend", "insert", "pop", "remove", "sort",
    "reverse", "copy", "items", "keys", "values", "get", "setdefault",
    "update", "add", "discard", "isoformat", "strftime", "utcnow", "now",
    "today",
})


def _call_excerpt(node: ast.Call, py_source: str) -> str:
    try:
        return _excerpt(ast.get_source_segment(py_source, node) or ast.dump(node))
    except Exception:
        return ""


def _receiver_root(node: ast.AST) -> str:
    """Leftmost name of an attribute chain ('sa.dialects.postgresql' -> 'sa');
    '' when the chain does not root at a plain name (e.g. a call result)."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _classify_alembic_call(node: ast.Call, source: str, py_source: str) -> List[LintFinding]:
    """Findings for ONE call node; [] means positively recognized as safe.
    `None` is never returned — unrecognized shapes yield non_op_execution."""
    def blocking(rule: str) -> List[LintFinding]:
        return [LintFinding(source, rule, True, _call_excerpt(node, py_source))]

    func = node.func
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name) and func.value.id == "op":
            if func.attr in _ALEMBIC_OP_RULES:
                return blocking(_ALEMBIC_OP_RULES[func.attr])
            if func.attr == "alter_column":
                findings: List[LintFinding] = []
                keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
                if "type_" in keywords:
                    findings += blocking("retype")
                nullable = keywords.get("nullable")
                if isinstance(nullable, ast.Constant) and nullable.value is False:
                    findings += blocking("set_not_null")
                if "new_column_name" in keywords:
                    findings += blocking("rename")
                return findings
            if func.attr == "execute":
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    # Literal SQL (adjacent-literal concatenation is folded by
                    # the parser) — classify like a raw-SQL migration statement.
                    return list(classify_sql(arg.value, source=f"{source}:execute").findings)
                # Variable / f-string / concatenation — opaque, fail closed.
                # The house loop style (0017's `for col in ...:
                # op.execute(f"...")`) lands here by design.
                return blocking("opaque_execute")
            if func.attr in _SAFE_OP_CALLS:
                return []
            # op.get_bind(), op.batch_alter_table(), constraint-creating op
            # helpers, anything this module does not model — fail closed.
            return blocking("non_op_execution")
        # Non-op receivers (batch/conn/session/...): the destructive alembic
        # names flag on ANY receiver (the batch_alter_table idiom); sa.* style
        # constructors and known-computational methods pass; everything else
        # (.execute() most importantly) fails closed.
        if func.attr in _ALEMBIC_OP_RULES:
            return blocking(_ALEMBIC_OP_RULES[func.attr])
        if _receiver_root(func.value) in _SAFE_CALL_ROOTS:
            return []
        if func.attr in _SAFE_METHOD_CALLS:
            return []
        return blocking("non_op_execution")

    if isinstance(func, ast.Name):
        if func.id in _SAFE_BUILTIN_CALLS:
            return []
        # In-file helper calls are resolved (and their bodies walked) by the
        # caller; a bare name that is neither is defined OUTSIDE the migration
        # — this module cannot see its body, so it may execute SQL.
        return blocking("non_op_execution")

    # func is itself a Call / Subscript / Lambda — dynamic dispatch, fail closed.
    return blocking("non_op_execution")


def classify_alembic_source(py_source: str, source: str = "<alembic>") -> MigrationClassification:
    """Only the upgrade() body counts — a drop in downgrade() must not flag.
    Fail-closed at two levels: (1) an unparseable source, or one with no
    upgrade(), is a blocking `unparseable_source` finding; (2) every call
    reachable from upgrade() must be positively recognized (see the module
    docstring for the recognized set) — in-file helper functions called from
    upgrade(), or passed as arguments there, are walked with the same rules;
    anything unrecognized is a blocking `non_op_execution` finding. The
    operator override at release creation is the escape hatch, never a
    silent pass."""
    try:
        tree = ast.parse(py_source)
    except SyntaxError:
        return _classification([LintFinding(source, "unparseable_source", True, "")])
    module_functions = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    upgrade = module_functions.get("upgrade")
    if upgrade is None:
        return _classification([LintFinding(source, "unparseable_source", True, "")])

    findings: List[LintFinding] = []
    walked = {"upgrade"}
    queue: List[ast.AST] = [upgrade]

    def enqueue(name: str) -> None:
        if name in module_functions and name not in walked:
            walked.add(name)
            queue.append(module_functions[name])

    while queue:
        scope = queue.pop(0)
        for node in ast.walk(scope):
            if not isinstance(node, ast.Call):
                continue
            # An in-file function passed callable-style to another call
            # (sorted(key=helper) executes it too) gets its body walked.
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(arg, ast.Name):
                    enqueue(arg.id)
            # A direct in-file helper call: walk the body instead of flagging.
            if isinstance(node.func, ast.Name) and node.func.id in module_functions:
                enqueue(node.func.id)
                continue
            findings.extend(_classify_alembic_call(node, source, py_source))
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
