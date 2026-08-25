"""The seed dataset must match the spec, on every machine, every time.

No database needed: generate_dataset() is pure, so its shape is testable here.
"""

from collections import Counter, defaultdict
from datetime import date

import pytest
import yaml

import seed_data
from config import settings
from db.dialect import get_dialect

ANCHOR = date(2026, 8, 24)


@pytest.fixture(scope="module")
def data():
    return seed_data.generate_dataset(anchor=ANCHOR, days=60, seed=20260101)


def test_is_deterministic():
    a = seed_data.generate_dataset(anchor=ANCHOR, days=60, seed=20260101)
    b = seed_data.generate_dataset(anchor=ANCHOR, days=60, seed=20260101)
    assert (a.batches, a.part_counts, a.alarms) == (b.batches, b.part_counts, b.alarms)


def test_volumes_are_in_the_expected_range(data):
    assert len(data.lines) == 3
    assert len(data.batches) >= 700
    assert len(data.part_counts) == 4 * len(data.batches)
    assert 1500 <= len(data.alarms) <= 2500


def test_three_to_six_batches_per_line_per_day(data):
    per_day = defaultdict(int)
    for _, line_id, _, start, _, _, _ in data.batches:
        per_day[(line_id, start.date())] += 1
    assert min(per_day.values()) >= 3
    assert max(per_day.values()) <= 6


def test_status_mix(data):
    counts = Counter(b[5] for b in data.batches)
    total = len(data.batches)
    assert set(counts) == {"completed", "aborted", "running"}
    assert 0.005 <= counts["running"] / total <= 0.02
    assert 0.01 <= counts["aborted"] / total <= 0.035


def test_running_batches_have_no_end_time(data):
    for batch in data.batches:
        if batch[5] == "running":
            assert batch[4] is None
        else:
            assert batch[4] is not None


def test_running_batches_are_the_most_recent(data):
    ordered = sorted(data.batches, key=lambda b: b[3])
    running_positions = [i for i, b in enumerate(ordered) if b[5] == "running"]
    assert min(running_positions) >= len(ordered) - 20


def test_some_batches_cross_midnight(data):
    """These exist to catch naive date filtering. There must be some."""
    overnight = [b for b in data.batches if b[4] and b[4].date() > b[3].date()]
    share = len(overnight) / len(data.batches)
    assert 0.03 <= share <= 0.09, f"{share:.1%} of batches cross midnight"
    assert all(b[3].hour >= 22 for b in overnight)


def test_batch_durations_are_sane(data):
    hours = [(b[4] - b[3]).total_seconds() / 3600 for b in data.batches if b[4]]
    assert min(hours) > 0
    assert max(hours) <= 4.5


def test_batch_ids_are_chronological_and_well_formed(data):
    ids = [b[0] for b in data.batches]
    assert ids == sorted(ids)
    assert all(len(i) == len("B-2026-000123") and i.startswith("B-") for i in ids)


def test_four_part_count_rows_per_batch(data):
    per_batch = defaultdict(set)
    for batch_id, category, _ in data.part_counts:
        per_batch[batch_id].add(category)
    expected = {"good", "reject_dimensional", "reject_visual", "reject_other"}
    assert len(per_batch) == len(data.batches)
    assert all(cats == expected for cats in per_batch.values())


def test_good_parts_are_90_to_98_percent(data):
    totals, good = defaultdict(int), {}
    for batch_id, category, count in data.part_counts:
        assert count >= 0
        totals[batch_id] += count
        if category == "good":
            good[batch_id] = count
    fractions = [good[b] / totals[b] for b in totals if totals[b] > 0]
    assert 0.895 <= min(fractions)
    assert max(fractions) <= 0.985


def test_reject_categories_rank_dimensional_visual_other(data):
    totals = defaultdict(int)
    for _, category, count in data.part_counts:
        if category != "good":
            totals[category] += count
    assert totals["reject_dimensional"] > totals["reject_visual"] > totals["reject_other"]


def test_output_tracks_target_and_is_sometimes_under(data):
    targets = {b[0]: b[6] for b in data.batches}
    statuses = {b[0]: b[5] for b in data.batches}
    totals = defaultdict(int)
    for batch_id, _, count in data.part_counts:
        totals[batch_id] += count
    ratios = [totals[b] / targets[b] for b in totals if statuses[b] == "completed"]
    assert 0.9 <= sum(ratios) / len(ratios) <= 1.05
    assert any(r < 1 for r in ratios)
    assert any(r > 1 for r in ratios)


def test_alarms_per_batch_never_exceed_five(data):
    per_batch = Counter(a[1] for a in data.alarms if a[1])
    assert max(per_batch.values()) <= 5
    assert len(per_batch) < len(data.batches)   # some batches have none


def test_about_ten_percent_of_alarms_are_standalone(data):
    standalone = sum(1 for a in data.alarms if a[1] is None)
    assert 0.06 <= standalone / len(data.alarms) <= 0.14


def test_ongoing_alarms_are_rare_and_only_on_running_batches(data):
    statuses = {b[0]: b[5] for b in data.batches}
    ongoing = [a for a in data.alarms if a[6] is None]
    assert 0 < len(ongoing) / len(data.alarms) <= 0.02
    assert all(statuses.get(a[1]) == "running" for a in ongoing)


def test_alarm_durations(data):
    seconds = [(a[6] - a[5]).total_seconds() for a in data.alarms if a[6]]
    assert min(seconds) >= 30
    assert max(seconds) <= 45 * 60
    long_ones = [s for s in seconds if s > 20 * 60]
    assert 0 < len(long_ones) / len(seconds) < 0.2


def test_alarm_code_maps_to_one_description_and_severity(data):
    seen = {}
    for _, _, code, description, severity, _, _ in data.alarms:
        seen.setdefault(code, (description, severity))
        assert seen[code] == (description, severity), f"{code} is inconsistent"
    assert set(seen) <= set(seed_data.ALARM_CATALOGUE)


def test_alarm_catalogue_matches_the_semantic_layer():
    """schema.yaml and the seeder must never drift apart."""
    with open(settings.schema_path, encoding="utf-8") as fh:
        catalogue = yaml.safe_load(fh)["alarm_catalogue"]
    assert set(catalogue) == set(seed_data.ALARM_CATALOGUE)
    for code, spec in catalogue.items():
        assert (spec["description"], spec["severity"]) == seed_data.ALARM_CATALOGUE[code]


def test_alarms_reference_real_batches_and_lines(data):
    batch_ids = {b[0] for b in data.batches}
    line_ids = {l[0] for l in data.lines}
    for line_id, batch_id, *_ in data.alarms:
        assert line_id in line_ids
        assert batch_id is None or batch_id in batch_ids


def test_products_are_in_range(data):
    codes = {b[2] for b in data.batches}
    assert codes <= {f"PRD-{n}" for n in range(100, 106)}


# --------------------------------------------------------------------------- #
# Insertion
# --------------------------------------------------------------------------- #

class _FakeCursor:
    """Captures the SQL the seeder would have executed."""

    def __init__(self):
        self.statements = []

    def executemany(self, sql, rows):
        self.statements.append(sql)


def test_insert_uses_the_driver_placeholder_not_the_neutral_one():
    """The seeder executes straight against the driver.

    It never passes through sql_guard, so there is no to_driver_sql() step to
    rewrite '?'. Emitting the authoring marker here is a syntax error that only
    shows up at `docker compose up` time.
    """
    dialect = get_dialect()
    cursor = _FakeCursor()
    seed_data._insert(cursor, dialect, "lines", ["line_id", "line_name"], [(1, "Line A")])

    sql = cursor.statements[0]
    assert dialect.driver_placeholder in sql
    if dialect.driver_placeholder != "?":
        assert "?" not in sql, f"neutral marker leaked into a driver statement: {sql}"


def test_insert_column_and_placeholder_counts_line_up():
    dialect = get_dialect()
    cursor = _FakeCursor()
    columns = ["batch_id", "line_id", "product_code", "start_time",
               "end_time", "status", "target_quantity"]
    seed_data._insert(cursor, dialect, "batches", columns, [tuple(range(len(columns)))])
    assert cursor.statements[0].count(dialect.driver_placeholder) == len(columns)


def test_insert_skips_empty_row_sets():
    cursor = _FakeCursor()
    seed_data._insert(cursor, get_dialect(), "lines", ["line_id"], [])
    assert cursor.statements == []


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def _settings(monkeypatch, **env):
    """A fresh Settings built from a controlled environment."""
    import config
    for key in ("POSTGRES_DB", "DB_HOST", "DB_PORT", "POSTGRES_ADMIN_USER",
                "POSTGRES_ADMIN_PASSWORD", "APP_READONLY_USER",
                "APP_READONLY_PASSWORD", "ADMIN_DATABASE_URL", "READONLY_DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return config.Settings()


def test_dsn_is_derived_from_the_discrete_variables(monkeypatch):
    s = _settings(monkeypatch, POSTGRES_DB="mfg", DB_HOST="db",
                  APP_READONLY_USER="readonly_user", APP_READONLY_PASSWORD="secret")
    assert s.readonly_dsn == "postgresql://readonly_user:secret@db:5432/mfg"


def test_dsn_percent_encodes_awkward_passwords(monkeypatch):
    """An unencoded '@' would split the URL in the wrong place."""
    s = _settings(monkeypatch, APP_READONLY_USER="readonly_user",
                  APP_READONLY_PASSWORD="p@ss:w/rd")
    assert "p%40ss%3Aw%2Frd" in s.readonly_dsn
    assert s.readonly_dsn.endswith("@db:5432/mfg")


def test_a_disagreeing_url_override_is_rejected_with_a_clear_message(monkeypatch):
    """This is the 'password authentication failed' failure, caught early."""
    s = _settings(
        monkeypatch,
        POSTGRES_ADMIN_USER="mfg_admin", POSTGRES_ADMIN_PASSWORD="admin-pw",
        APP_READONLY_USER="readonly_user", APP_READONLY_PASSWORD="the-real-password",
        READONLY_DATABASE_URL="postgresql://readonly_user:a-stale-password@db:5432/mfg",
    )
    with pytest.raises(RuntimeError) as excinfo:
        s.require_db()
    message = str(excinfo.value)
    assert "READONLY_DATABASE_URL" in message
    assert "APP_READONLY_PASSWORD" in message


def test_an_agreeing_url_override_is_accepted(monkeypatch):
    s = _settings(
        monkeypatch,
        POSTGRES_ADMIN_USER="mfg_admin", POSTGRES_ADMIN_PASSWORD="admin-pw",
        APP_READONLY_USER="readonly_user", APP_READONLY_PASSWORD="pw",
        READONLY_DATABASE_URL="postgresql://readonly_user:pw@db:5432/mfg",
    )
    s.require_db()


def test_missing_credentials_are_named(monkeypatch):
    s = _settings(monkeypatch, POSTGRES_ADMIN_USER="mfg_admin")
    with pytest.raises(RuntimeError) as excinfo:
        s.require_db()
    assert "POSTGRES_ADMIN_PASSWORD" in str(excinfo.value)
