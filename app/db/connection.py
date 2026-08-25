"""Connection helpers. The only module that opens a database connection.

Two distinct paths, deliberately kept apart:
  * `readonly_connection()` — everything the chatbot does. SELECT-only role,
    server-side statement timeout, read-only session.
  * `admin_connection()`    — the seed step, and nothing else.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from config import settings

from .dialect import Dialect, get_dialect

log = logging.getLogger(__name__)

TABLES = ["lines", "batches", "part_counts", "alarms"]


@dataclass
class QueryResult:
    """Everything the UI needs to show what happened."""

    columns: list[str]
    rows: list[tuple]
    execution_ms: float
    sql: str = ""
    truncated: bool = False
    params: list[Any] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]


class QueryExecutionError(RuntimeError):
    """The database rejected or could not finish the query."""


@contextmanager
def readonly_connection(dialect: Dialect | None = None) -> Iterator[Any]:
    dialect = dialect or get_dialect()
    settings.require_db()
    conn = dialect.connect_readonly(settings.readonly_dsn, settings.statement_timeout_ms)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def admin_connection(dialect: Dialect | None = None) -> Iterator[Any]:
    dialect = dialect or get_dialect()
    settings.require_db()
    conn = dialect.connect_admin(settings.admin_dsn)
    try:
        yield conn
    finally:
        conn.close()


def run_select(
    sql: str,
    params: Sequence[Any] = (),
    dialect: Dialect | None = None,
) -> QueryResult:
    """Execute a validated SELECT on the readonly connection and time it.

    This function trusts that `sql` has already been through sql_guard. It is
    not a second line of defence — the readonly role is.
    """
    dialect = dialect or get_dialect()
    # The guard validated '?' markers; the driver wants its own. This is the
    # only place the two representations meet.
    driver_sql = dialect.to_driver_sql(sql)
    started = time.perf_counter()
    try:
        with readonly_connection(dialect) as conn, conn.cursor() as cur:
            cur.execute(driver_sql, tuple(params))
            columns = [d[0] for d in (cur.description or [])]
            rows = [tuple(r) for r in cur.fetchall()]
    except Exception as exc:  # driver-specific types stay inside this module
        raise QueryExecutionError(str(exc).strip()) from exc

    elapsed_ms = (time.perf_counter() - started) * 1000
    return QueryResult(
        columns=columns,
        rows=rows,
        execution_ms=round(elapsed_ms, 1),
        sql=sql,
        truncated=len(rows) >= settings.max_rows,
        params=list(params),
    )


def check_database_ready(dialect: Dialect | None = None) -> tuple[bool, str]:
    """Readiness probe: can the READONLY role reach every seeded table?

    Deliberately uses the readonly connection — that is the one the chatbot
    depends on, so a missing GRANT shows up here rather than at demo time.
    """
    dialect = dialect or get_dialect()
    try:
        with readonly_connection(dialect) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
            for table in TABLES:
                cur.execute(f"SELECT COUNT(*) FROM {dialect.quote_identifier(table)}")
                (count,) = cur.fetchone()
                if table == "batches" and not count:
                    return False, "the batches table is empty — has the seed step run?"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc).strip()}"
    return True, "database reachable and seeded"
