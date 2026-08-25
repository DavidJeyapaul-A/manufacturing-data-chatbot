"""The readonly role: creation, repair, and safe SQL composition.

This path has no unit-testable database, but the SQL it composes is exactly
where a quoting mistake becomes a privilege bug, so the statements are asserted
directly against a fake connection.
"""

import pytest

from db.dialect import get_dialect


class _FakeCursor:
    def __init__(self, role_exists: bool):
        self._role_exists = role_exists
        self.executed: list[tuple[str, tuple | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        text = query.as_string(None) if hasattr(query, "as_string") else query
        self.executed.append((text, params))

    def fetchone(self):
        return (1,) if self._role_exists else None


class _FakeConnection:
    def __init__(self, role_exists: bool):
        self._cursor = _FakeCursor(role_exists)

    def cursor(self):
        return self._cursor


def _run(role_exists: bool, user="readonly_user", password="pw", db="mfg"):
    dialect = get_dialect()
    conn = _FakeConnection(role_exists)
    returned = dialect.ensure_readonly_role(conn, user, password, db)
    return returned, [sql for sql, _ in conn._cursor.executed]


def test_missing_role_is_created():
    existed, statements = _run(role_exists=False)
    assert existed is False
    assert any(s.startswith("CREATE ROLE") for s in statements)
    assert not any(s.startswith("ALTER ROLE") for s in statements)


def test_existing_role_has_its_password_reapplied():
    """So changing a password in .env does not require recreating the volume."""
    existed, statements = _run(role_exists=True)
    assert existed is True
    assert any(s.startswith("ALTER ROLE") for s in statements)
    assert not any(s.startswith("CREATE ROLE") for s in statements)


@pytest.mark.parametrize("role_exists", [True, False])
def test_role_is_granted_connect_usage_and_nothing_else(role_exists):
    _, statements = _run(role_exists=role_exists)
    joined = " | ".join(statements)
    assert "GRANT CONNECT ON DATABASE" in joined
    assert "GRANT USAGE ON SCHEMA public" in joined
    assert "REVOKE CREATE ON SCHEMA public" in joined
    # Nothing here may hand out write privileges.
    for forbidden in ("INSERT", "UPDATE", "DELETE", "ALL PRIVILEGES", "SUPERUSER",
                      "CREATEDB", "CREATEROLE"):
        assert forbidden not in joined.upper()


@pytest.mark.parametrize("role_exists", [True, False])
def test_role_never_inherits_privileges(role_exists):
    _, statements = _run(role_exists=role_exists)
    role_stmt = next(s for s in statements if "ROLE" in s and "GRANT" not in s)
    assert "NOINHERIT" in role_stmt
    assert "LOGIN" in role_stmt


def test_identifiers_and_literals_are_escaped():
    """Identifiers cannot be bound as parameters, so composition must escape."""
    _, statements = _run(role_exists=False, user='we"ird', password="p'ass", db="my-db")
    create = next(s for s in statements if s.startswith("CREATE ROLE"))
    assert '"we""ird"' in create        # doubled quote inside a quoted identifier
    assert "'p''ass'" in create         # doubled apostrophe inside a literal
    assert '"my-db"' in " | ".join(statements)


def test_role_name_is_looked_up_as_a_bound_parameter():
    dialect = get_dialect()
    conn = _FakeConnection(False)
    dialect.ensure_readonly_role(conn, "readonly_user", "pw", "mfg")
    lookup, params = conn._cursor.executed[0]
    assert "pg_roles" in lookup
    assert params == ("readonly_user",)
