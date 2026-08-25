"""SQL safety guardrails. Applied to BOTH query paths, before any execution.

Layers, outermost first:
  1. Parse with sqlglot. Unparseable => rejected.
  2. Exactly one statement, and it must be a SELECT.
  3. No write/DDL/administrative node anywhere in the tree.
  4. Every table and column must appear in the schema.yaml whitelist.
  5. Only allow-listed scalar/aggregate functions.
  6. A row cap is injected, or clamped if the query asked for more.
  7. The SQL that finally runs is REGENERATED FROM THE PARSE TREE, not the
     original text — so anything the parser did not understand cannot survive.

Behind all of that sits the real guarantee: execution happens on a connection
owned by a role with SELECT and nothing else. This module is what turns a bad
query into a clear message instead of a database error.

Deliberately absent: any attempt to repair invalid SQL. A rejected query is
reported, never rewritten into something that "probably" means the same thing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from config import settings
from db.dialect import Dialect, get_dialect
from semantic import load_semantic_model

log = logging.getLogger(__name__)


class SqlRejected(Exception):
    """The statement failed validation and must not be executed."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass
class GuardedSql:
    sql: str
    row_cap: int
    notes: list[str]


# --- 3. Node types that must never appear ----------------------------------
# Resolved by name so a sqlglot upgrade that adds or renames a node type cannot
# silently drop a rule (a missing name is skipped, not a crash).
_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Merge", "Drop", "Create", "Alter",
    "TruncateTable", "Grant", "Revoke", "Command", "Transaction", "Commit",
    "Rollback", "Use", "Set", "SetItem", "Copy", "Into", "Call", "Pragma",
    "Attach", "Detach", "AlterTable", "RenameTable", "Analyze", "Vacuum",
    "Refresh", "Export", "LoadData", "Fetch", "Lock", "Prepare", "Execute",
)
FORBIDDEN_NODES = tuple(
    getattr(exp, name) for name in _FORBIDDEN_NODE_NAMES if hasattr(exp, name)
)

# --- 5. Functions sqlglot does not model as a typed node -------------------
# Typed nodes (COUNT, SUM, DATE_TRUNC, EXTRACT, ...) are read-only by
# construction. Everything else arrives as exp.Anonymous, and only these are
# allowed through — which is what keeps pg_sleep(), pg_read_file(), dblink()
# and friends out.
ALLOWED_ANONYMOUS_FUNCTIONS = frozenset(
    {
        "age", "btrim", "ceil", "ceiling", "concat_ws", "date_part", "dateadd",
        "datediff", "datepart", "floor", "format", "getdate", "greatest",
        "initcap", "justify_interval", "least", "mod", "percentile_cont",
        "percentile_disc", "power", "regexp_replace", "sign", "split_part",
        "sqrt", "string_agg", "strpos", "to_char", "trunc", "width_bucket",
    }
)

# Schemas a table may be qualified with. Blocks pg_catalog / information_schema
# even before the table whitelist gets a look at them.
ALLOWED_SCHEMAS = frozenset({"", "public", "dbo"})

_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def strip_markdown_fence(text: str) -> str:
    """Pull SQL out of a ```sql fenced block if the model wrapped it in one."""
    match = _FENCE_RE.search(text or "")
    return (match.group(1) if match else (text or "")).strip()


def validate(
    sql: str,
    dialect: Dialect | None = None,
    max_rows: int | None = None,
) -> GuardedSql:
    """Validate and normalise a statement. Raises SqlRejected on any violation."""
    dialect = dialect or get_dialect()
    cap = max_rows if max_rows is not None else settings.max_rows
    model = load_semantic_model()
    notes: list[str] = []

    candidate = (sql or "").strip()
    if not candidate:
        raise SqlRejected("no SQL was produced")

    # ---- 1. parse ---------------------------------------------------------
    try:
        statements = [s for s in dialect.parse(candidate) if s is not None]
    except ParseError as exc:
        raise SqlRejected(
            "the SQL could not be parsed", _first_line(str(exc))
        ) from exc

    # ---- 2. exactly one SELECT -------------------------------------------
    if not statements:
        raise SqlRejected("no SQL statement was found")
    if len(statements) > 1:
        raise SqlRejected(
            f"expected a single statement but found {len(statements)}"
        )

    statement = statements[0]
    if not isinstance(statement, exp.Select):
        kind = type(statement).__name__.upper()
        raise SqlRejected(f"only SELECT statements are allowed (got {kind})")

    # ---- 3. no write / DDL / admin nodes ---------------------------------
    for node in statement.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SqlRejected(
                f"the statement contains a disallowed operation ({type(node).__name__.upper()})"
            )

    # ---- 4. table + column whitelist -------------------------------------
    # Names the query defines for itself rather than reading from the schema:
    # CTEs (WITH x AS ...) and derived tables (FROM (SELECT ...) x). Columns
    # coming out of these cannot be whitelist-checked by name against a real
    # table, but everything they read from IS checked, so the boundary holds.
    virtual_names = {
        cte.alias_or_name.lower()
        for cte in statement.find_all(exp.CTE)
        if cte.alias_or_name
    } | {
        sub.alias.lower()
        for sub in statement.find_all(exp.Subquery)
        if getattr(sub, "alias", "")
    }
    alias_to_table = _check_tables(statement, model.table_names, virtual_names)
    _check_columns(statement, model, alias_to_table, virtual_names)

    # ---- 5. function allowlist -------------------------------------------
    for func in statement.find_all(exp.Anonymous):
        fname = (func.name or "").lower()
        if fname not in ALLOWED_ANONYMOUS_FUNCTIONS:
            raise SqlRejected(f"the function {fname or '?'}() is not allowed")

    # ---- 6. row cap -------------------------------------------------------
    before = statement.sql(dialect=dialect.sqlglot_dialect)
    statement = dialect.apply_row_cap(statement, cap)
    after = statement.sql(dialect=dialect.sqlglot_dialect)
    if before != after:
        notes.append(f"row limit set to {cap}")

    # ---- 7. regenerate ----------------------------------------------------
    return GuardedSql(sql=dialect.generate(statement), row_cap=cap, notes=notes)


def _check_tables(
    statement: exp.Expression,
    allowed_tables: frozenset[str],
    virtual_names: set[str],
) -> dict[str, str]:
    """Verify every table reference; return {alias_or_name: real_table_name}."""
    alias_to_table: dict[str, str] = {}

    for table in statement.find_all(exp.Table):
        name = (table.name or "").lower()
        schema = (table.db or "").lower()

        if schema not in ALLOWED_SCHEMAS:
            raise SqlRejected(f"the schema '{schema}' is not accessible")
        if name not in virtual_names and name not in allowed_tables:
            raise SqlRejected(
                f"the table '{name}' is not in the allowed schema",
                f"allowed tables: {', '.join(sorted(allowed_tables))}",
            )
        alias_to_table[(table.alias or name).lower()] = name

    if not alias_to_table:
        raise SqlRejected("the query does not read from any known table")
    return alias_to_table


def _check_columns(
    statement: exp.Expression,
    model: Any,
    alias_to_table: dict[str, str],
    virtual_names: set[str],
) -> None:
    """Verify every column reference against the whitelist.

    Name-level checking, not full resolution: a column is accepted if it exists
    on one of the tables the query actually references, or is a name the query
    itself defined (a select alias, CTE or derived table). That is the right
    strength here because the table whitelist already bounds which tables can be
    reached at all — there is no name that reaches data outside the four tables.
    """
    # Names the query invents for itself: SELECT ... AS x, then ORDER BY x.
    local_names = {
        node.alias.lower()
        for node in statement.find_all(exp.Alias)
        if node.alias
    } | virtual_names | set(alias_to_table)

    referenced_tables = {t for t in alias_to_table.values() if t not in virtual_names}
    reachable_columns: set[str] = set()
    for table in referenced_tables:
        reachable_columns |= {c.lower() for c in model.columns_of(table)}

    for column in statement.find_all(exp.Column):
        cname = (column.name or "").lower()
        if not cname or cname == "*":
            continue

        qualifier = (column.table or "").lower()
        if qualifier:
            if qualifier in virtual_names:
                continue  # produced by a CTE / derived table defined right here
            if qualifier not in alias_to_table:
                raise SqlRejected(f"unknown table alias '{qualifier}'")
            source = alias_to_table[qualifier]
            if source in virtual_names:
                continue
            if cname not in {c.lower() for c in model.columns_of(source)}:
                raise SqlRejected(f"'{source}.{cname}' is not a column in the allowed schema")
            continue

        if cname in reachable_columns or cname in local_names:
            continue
        raise SqlRejected(f"'{cname}' is not a column in the allowed schema")


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0] if text else ""


__all__ = ["validate", "SqlRejected", "GuardedSql", "strip_markdown_fence"]
