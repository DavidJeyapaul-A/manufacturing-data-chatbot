"""Database access layer.

Everything Postgres-specific in this project lives in this package. The rest of
the app talks to `get_dialect()` and `connection.run_select()` and never imports
a driver or writes vendor SQL. See README "Migrating to SQL Server".
"""

from .dialect import Dialect, get_dialect

__all__ = ["Dialect", "get_dialect"]
