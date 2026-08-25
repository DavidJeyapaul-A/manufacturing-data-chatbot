"""The one place database-vendor differences are allowed to exist.

Adding SQL Server later means writing a second subclass here and swapping one
env var. Nothing else in the app should need to change — if you find yourself
writing `LIMIT` or `EXTRACT(EPOCH ...)` anywhere else, it belongs in here.

Each dialect owns five things:
  1. the driver and how a connection is opened (incl. the statement timeout)
  2. the sqlglot dialect string used to parse and re-generate SQL
  3. the row-cap syntax (LIMIT n / TOP n)
  4. the date and duration expressions the templates need
  5. which few-shot example file to feed the LLM
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import sqlglot
from sqlglot import exp


class Dialect(ABC):
    """Contract every supported database must satisfy."""

    #: Human-readable name, matches the DB_DIALECT env var.
    name: str
    #: Dialect string handed to sqlglot.parse / .sql — 'postgres', 'tsql', ...
    sqlglot_dialect: str
    #: Marker templates AUTHOR bound parameters with. Deliberately '?' for every
    #: dialect: it is the one marker sqlglot can parse, and the guard has to
    #: parse the SQL before the values are bound. Converted to the driver's own
    #: marker by to_driver_sql() at the moment of execution.
    placeholder: str = "?"
    #: What the DRIVER expects: '%s' for psycopg, '?' for pyodbc.
    driver_placeholder: str
    #: File under app/fewshot/ holding the question -> SQL examples.
    fewshot_file: str

    # -- SQL fragments the templates need --------------------------------

    @abstractmethod
    def duration_seconds(self, start_expr: str, end_expr: str) -> str:
        """SQL expression for (end - start) in seconds."""

    @abstractmethod
    def truncate_to_day(self, expr: str) -> str:
        """SQL expression reducing a timestamp to its calendar day."""

    @abstractmethod
    def quote_identifier(self, ident: str) -> str:
        """Quote an identifier that may collide with a reserved word."""

    # -- Row cap ----------------------------------------------------------

    def apply_row_cap(self, expression: exp.Expression, cap: int) -> exp.Expression:
        """Ensure the statement returns at most `cap` rows.

        Works on the parsed tree, so the vendor syntax is produced at generate
        time: `LIMIT n` for Postgres, `SELECT TOP n` for SQL Server. That is
        why this base implementation is shared rather than overridden.
        """
        existing = expression.args.get("limit")
        if existing is None:
            return expression.limit(cap)

        current = _literal_int(existing.args.get("expression"))
        if current is not None and current <= cap:
            return expression
        return expression.limit(cap)

    def generate(self, expression: exp.Expression) -> str:
        """Render a parsed tree back to SQL text in this dialect."""
        return expression.sql(dialect=self.sqlglot_dialect, pretty=True)

    def with_limit(self, sql: str, n: int) -> str:
        """Parse `sql`, apply a row cap of `n`, and render it back.

        Used by templates.py to turn a requested "top N" into the right vendor
        syntax (LIMIT n / TOP n) without templates.py knowing which one that is.
        """
        parsed = sqlglot.parse_one(sql, read=self.sqlglot_dialect)
        capped = self.apply_row_cap(parsed, n)
        return self.generate(capped)

    def parse(self, sql: str) -> list[exp.Expression | None]:
        """Parse SQL text. Raises sqlglot.ParseError on malformed input."""
        return sqlglot.parse(sql, read=self.sqlglot_dialect)

    def to_driver_sql(self, sql: str) -> str:
        """Rewrite the neutral '?' markers into the driver's own marker.

        Done on the PARSE TREE, not by string replacement, so a literal question
        mark inside a quoted string is left alone. This is the last step before
        execution: everything upstream — including the guard and the SQL shown
        in the UI — works with '?'.
        """
        if self.driver_placeholder == "?":
            return sql
        tree = sqlglot.parse_one(sql, read=self.sqlglot_dialect)
        for node in list(tree.find_all(exp.Placeholder)):
            node.replace(exp.Var(this=self.driver_placeholder))
        return self.generate(tree)

    # -- Connections ------------------------------------------------------

    @abstractmethod
    def connect_readonly(self, dsn: str, statement_timeout_ms: int) -> Any:
        """Open the SELECT-only connection used by the chatbot query path."""

    @abstractmethod
    def connect_admin(self, dsn: str) -> Any:
        """Open the privileged connection used ONLY by the seeding step."""

    # -- DDL / grants used by the seed step -------------------------------

    @abstractmethod
    def create_schema_statements(self) -> list[str]:
        """CREATE TABLE statements, in dependency order."""

    @abstractmethod
    def grant_readonly_statements(self, readonly_user: str, tables: list[str]) -> list[str]:
        """GRANT SELECT on the seeded tables to the readonly role."""


def _literal_int(node: Any) -> int | None:
    """Best-effort read of an integer out of a sqlglot literal node."""
    if node is None:
        return None
    try:
        return int(node.name)
    except (AttributeError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# PostgreSQL — the POC database
# ---------------------------------------------------------------------------
class PostgresDialect(Dialect):
    name = "postgres"
    sqlglot_dialect = "postgres"
    driver_placeholder = "%s"          # psycopg
    fewshot_file = "postgres.yaml"

    def duration_seconds(self, start_expr: str, end_expr: str) -> str:
        return f"EXTRACT(EPOCH FROM ({end_expr} - {start_expr}))"

    def truncate_to_day(self, expr: str) -> str:
        return f"DATE_TRUNC('day', {expr})"

    def quote_identifier(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def connect_readonly(self, dsn: str, statement_timeout_ms: int) -> Any:
        import psycopg

        conn = psycopg.connect(
            dsn,
            # Server-side kill switch: a runaway query cannot pin a CPU.
            options=f"-c statement_timeout={int(statement_timeout_ms)}",
            connect_timeout=5,
            autocommit=True,
        )
        # Defence in depth. The role has no write privileges anyway, but this
        # makes an attempted write fail at the session level too.
        conn.read_only = True
        return conn

    def connect_admin(self, dsn: str) -> Any:
        import psycopg

        return psycopg.connect(dsn, connect_timeout=5)

    def create_schema_statements(self) -> list[str]:
        return [
            """
            CREATE TABLE IF NOT EXISTS lines (
                line_id    INT PRIMARY KEY,
                line_name  TEXT NOT NULL,
                location   TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS batches (
                batch_id        TEXT PRIMARY KEY,
                line_id         INT  NOT NULL REFERENCES lines(line_id),
                product_code    TEXT NOT NULL,
                start_time      TIMESTAMP NOT NULL,
                end_time        TIMESTAMP,
                status          TEXT NOT NULL,
                target_quantity INT  NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS part_counts (
                id        SERIAL PRIMARY KEY,
                batch_id  TEXT NOT NULL REFERENCES batches(batch_id),
                category  TEXT NOT NULL,
                count     INT  NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS alarms (
                alarm_id     SERIAL PRIMARY KEY,
                line_id      INT  NOT NULL REFERENCES lines(line_id),
                batch_id     TEXT REFERENCES batches(batch_id),
                alarm_code   TEXT NOT NULL,
                description  TEXT NOT NULL,
                severity     TEXT NOT NULL,
                start_time   TIMESTAMP NOT NULL,
                end_time     TIMESTAMP
            )
            """,
            # Indexes matching how the templates filter: by line and by time.
            "CREATE INDEX IF NOT EXISTS ix_batches_line_start ON batches (line_id, start_time)",
            "CREATE INDEX IF NOT EXISTS ix_part_counts_batch ON part_counts (batch_id)",
            "CREATE INDEX IF NOT EXISTS ix_alarms_line_start ON alarms (line_id, start_time)",
            "CREATE INDEX IF NOT EXISTS ix_alarms_batch ON alarms (batch_id)",
        ]

    def grant_readonly_statements(self, readonly_user: str, tables: list[str]) -> list[str]:
        user = self.quote_identifier(readonly_user)
        stmts = [f"GRANT USAGE ON SCHEMA public TO {user}"]
        stmts += [
            f"GRANT SELECT ON {self.quote_identifier(t)} TO {user}" for t in tables
        ]
        # part_counts.id / alarms.alarm_id are SERIAL; reading is enough, but the
        # sequence grant keeps \d and tooling from erroring on introspection.
        stmts.append(f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {user}")
        return stmts


# ---------------------------------------------------------------------------
# Microsoft SQL Server — the client's production database.
#
# Left deliberately as a documented stub. To switch:
#   1. add `pyodbc` to requirements.txt (plus the msodbcsql18 driver in the
#      Dockerfile — that is the only image change)
#   2. delete the NotImplementedError bodies below
#   3. set DB_DIALECT=mssql in .env and point the two DSNs at SQL Server
#   4. copy app/fewshot/postgres.yaml to tsql.yaml and adjust the example SQL
# Everything else — templates, guard, responder, UI — is untouched.
# ---------------------------------------------------------------------------
class SqlServerDialect(Dialect):
    name = "mssql"
    sqlglot_dialect = "tsql"
    driver_placeholder = "?"           # pyodbc — already the neutral marker
    fewshot_file = "tsql.yaml"

    def duration_seconds(self, start_expr: str, end_expr: str) -> str:
        return f"DATEDIFF(SECOND, {start_expr}, {end_expr})"

    def truncate_to_day(self, expr: str) -> str:
        return f"CAST({expr} AS DATE)"

    def quote_identifier(self, ident: str) -> str:
        return "[" + ident.replace("]", "]]") + "]"

    def connect_readonly(self, dsn: str, statement_timeout_ms: int) -> Any:
        raise NotImplementedError(
            "SQL Server support is a stub. See the block comment above this class "
            "and the README section 'Migrating to SQL Server'."
        )

    def connect_admin(self, dsn: str) -> Any:
        raise NotImplementedError("SQL Server support is a stub.")

    def create_schema_statements(self) -> list[str]:
        raise NotImplementedError(
            "SQL Server DDL differs: TEXT -> NVARCHAR(MAX), SERIAL -> INT IDENTITY(1,1), "
            "TIMESTAMP -> DATETIME2. In production the tables already exist, so the seed "
            "step is Postgres/POC-only anyway."
        )

    def grant_readonly_statements(self, readonly_user: str, tables: list[str]) -> list[str]:
        # T-SQL equivalent, for reference:
        #   CREATE LOGIN readonly_user WITH PASSWORD = '...';
        #   CREATE USER  readonly_user FOR LOGIN readonly_user;
        #   ALTER ROLE   db_datareader ADD MEMBER readonly_user;
        raise NotImplementedError("Use db_datareader role membership on SQL Server.")


_DIALECTS: dict[str, type[Dialect]] = {
    "postgres": PostgresDialect,
    "postgresql": PostgresDialect,
    "mssql": SqlServerDialect,
    "sqlserver": SqlServerDialect,
    "tsql": SqlServerDialect,
}


def get_dialect(name: str | None = None) -> Dialect:
    """Resolve the configured dialect. Defaults to DB_DIALECT in the env."""
    if name is None:
        from config import settings

        name = settings.dialect_name
    key = (name or "postgres").strip().lower()
    if key not in _DIALECTS:
        raise ValueError(
            f"Unknown DB_DIALECT {name!r}. Supported: {', '.join(sorted(set(_DIALECTS)))}"
        )
    return _DIALECTS[key]()
