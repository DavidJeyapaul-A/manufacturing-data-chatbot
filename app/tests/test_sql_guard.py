"""The guard is the security boundary. These are its adversarial tests."""

import pytest

import sql_guard
from sql_guard import SqlRejected, validate


# --------------------------------------------------------------------------- #
# Things that must be REJECTED
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sql", [
    # not a SELECT
    "DELETE FROM batches",
    "UPDATE batches SET status = 'completed'",
    "INSERT INTO lines VALUES (4, 'Line D', 'x')",
    "DROP TABLE alarms",
    "TRUNCATE TABLE alarms",
    "CREATE TABLE evil (id INT)",
    "ALTER TABLE batches ADD COLUMN x INT",
    "GRANT SELECT ON batches TO PUBLIC",
    # stacked statements — the classic injection shape
    "SELECT 1 FROM batches; DROP TABLE batches",
    "SELECT * FROM batches; DELETE FROM alarms",
    # a write hidden inside a CTE
    "WITH x AS (DELETE FROM alarms RETURNING *) SELECT * FROM x",
    # tables outside the whitelist
    "SELECT * FROM pg_catalog.pg_tables",
    "SELECT * FROM information_schema.tables",
    "SELECT usename, passwd FROM pg_shadow",
    "SELECT * FROM secret_table",
    "SELECT * FROM public.users",
    # columns outside the whitelist
    "SELECT password FROM batches",
    "SELECT b.salary FROM batches b",
    # dangerous functions
    "SELECT pg_sleep(10) FROM batches",
    "SELECT pg_read_file('/etc/passwd') FROM batches",
    "SELECT dblink('host=evil', 'SELECT 1') FROM batches",
    # nothing to read from
    "SELECT 1",
    # not SQL at all
    "hello, how are you?",
    "",
])
def test_rejects(sql):
    with pytest.raises(SqlRejected):
        validate(sql)


def test_rejection_carries_a_readable_reason():
    with pytest.raises(SqlRejected) as excinfo:
        validate("DELETE FROM batches")
    assert excinfo.value.reason
    assert "SELECT" in excinfo.value.reason or "allowed" in excinfo.value.reason


def test_guard_never_repairs_sql():
    """A rejected query is reported, never rewritten into something 'close'."""
    with pytest.raises(SqlRejected):
        validate("SELECT nonexistent_column FROM batches")


# --------------------------------------------------------------------------- #
# Things that must be ALLOWED
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sql", [
    "SELECT * FROM batches",
    "SELECT batch_id, status FROM batches WHERE status = 'running'",
    'SELECT SUM(pc."count") FROM part_counts pc',
    "SELECT l.line_name, COUNT(*) FROM alarms a JOIN lines l ON l.line_id = a.line_id GROUP BY l.line_name",
    "SELECT DATE_TRUNC('day', start_time) AS d, COUNT(*) FROM batches GROUP BY DATE_TRUNC('day', start_time)",
    "SELECT EXTRACT(EPOCH FROM (end_time - start_time)) FROM alarms WHERE end_time IS NOT NULL",
    "WITH t AS (SELECT batch_id FROM batches) SELECT batch_id FROM t",
    "SELECT b.batch_id FROM batches b WHERE NOT EXISTS (SELECT 1 FROM alarms a WHERE a.batch_id = b.batch_id)",
])
def test_allows(sql):
    assert validate(sql).sql


# --------------------------------------------------------------------------- #
# Row cap
# --------------------------------------------------------------------------- #

def test_row_cap_is_injected_when_missing():
    out = validate("SELECT * FROM batches", max_rows=50)
    assert "LIMIT 50" in out.sql.upper()
    assert out.notes


def test_row_cap_clamps_a_larger_limit():
    out = validate("SELECT * FROM batches LIMIT 100000", max_rows=50)
    assert "LIMIT 50" in out.sql.upper()
    assert "100000" not in out.sql


def test_row_cap_leaves_a_smaller_limit_alone():
    out = validate("SELECT * FROM batches LIMIT 5", max_rows=50)
    assert "LIMIT 5" in out.sql.upper()
    assert not out.notes


# --------------------------------------------------------------------------- #
# LLM output cleanup
# --------------------------------------------------------------------------- #

def test_strips_markdown_fence():
    assert sql_guard.strip_markdown_fence("```sql\nSELECT 1\n```") == "SELECT 1"
    assert sql_guard.strip_markdown_fence("SELECT 1") == "SELECT 1"


def test_sql_is_regenerated_from_the_parse_tree():
    """What executes comes from the tree, so unparsed text cannot survive."""
    out = validate("select   batch_id   from batches")
    assert "SELECT" in out.sql
    assert "select   batch_id" not in out.sql
