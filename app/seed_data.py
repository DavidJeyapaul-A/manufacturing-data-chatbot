"""Creates the schema, generates the demo dataset, and grants SELECT.

Run as the one-shot `seed` service, using the ADMIN role — the only place in
this project that connects with write privileges.

Two guarantees:

  * IDEMPOTENT. If `batches` already has rows, no data is written. The grants
    are re-applied every run regardless, so a re-run repairs a missing GRANT
    without touching the data.
  * REPRODUCIBLE. A fixed RNG seed (SEED_RANDOM_SEED) means every machine
    generates the identical dataset. Pin SEED_END_DATE as well and the rows are
    byte-identical, timestamps included.

Generation is deliberately separate from insertion — `generate_dataset()` is a
pure function of (anchor_date, days, seed), so the shape of the data is tested
without needing a database.
"""

from __future__ import annotations

import logging
import math
import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from config import settings
from db.connection import TABLES, admin_connection
from db.dialect import Dialect, get_dialect

logging.basicConfig(level=logging.INFO, format="[seed] %(message)s")
log = logging.getLogger("seed")


# --------------------------------------------------------------------------- #
# Fixed reference data
# --------------------------------------------------------------------------- #

LINES: tuple[tuple[int, str, str], ...] = (
    (1, "Line A", "Building 1 - Bay 3"),
    (2, "Line B", "Building 1 - Bay 4"),
    (3, "Line C", "Building 2 - Bay 1"),
)

#: code -> (description, severity). Mirrors schema.yaml's alarm_catalogue, and
#: tests/test_seed_shape.py asserts the two never drift apart.
ALARM_CATALOGUE: dict[str, tuple[str, str]] = {
    "ALM-01": ("Infeed conveyor jam", "warning"),
    "ALM-02": ("Emergency stop pressed", "critical"),
    "ALM-03": ("Low air pressure", "warning"),
    "ALM-04": ("Vision system fault", "warning"),
    "ALM-05": ("Labeller misfeed", "warning"),
    "ALM-06": ("Guard door open", "critical"),
    "ALM-07": ("Temperature out of range", "warning"),
    "ALM-08": ("Reject bin full", "info"),
    "ALM-09": ("Product changeover", "info"),
    "ALM-10": ("Motor overload", "critical"),
    "ALM-11": ("Sensor communication lost", "warning"),
    "ALM-12": ("Scheduled maintenance", "info"),
}

#: Some alarms are simply more common than others on a real line.
ALARM_WEIGHTS: dict[str, float] = {
    "ALM-01": 3.0, "ALM-03": 2.5, "ALM-05": 2.2, "ALM-04": 2.0,
    "ALM-08": 1.8, "ALM-09": 1.6, "ALM-11": 1.4, "ALM-07": 1.2,
    "ALM-02": 0.8, "ALM-06": 0.7, "ALM-10": 0.6, "ALM-12": 0.4,
}

#: product -> nominal parts per hour. Drives target_quantity.
PRODUCTS: dict[str, int] = {
    "PRD-100": 240, "PRD-101": 180, "PRD-102": 300,
    "PRD-103": 150, "PRD-104": 210, "PRD-105": 270,
}

REJECT_CATEGORIES = ("reject_dimensional", "reject_visual", "reject_other")
#: dimensional most common, then visual, then other.
REJECT_WEIGHTS = (0.55, 0.30, 0.15)

# Tunables. Named so the generated volumes are traceable to a decision.
BATCHES_PER_LINE_PER_DAY = (3, 6)
BATCH_HOURS = (1.0, 4.0)
MIN_BATCH_HOURS = 0.75
GAP_MINUTES = (15, 90)
MIN_GAP_MINUTES = 15
SHIFT_START_HOUR = 6
#: A day's batches are packed between SHIFT_START_HOUR and here. Without this
#: ceiling a run of six long batches drifts past midnight and lands in the next
#: day's counts, which would break the '3-6 batches per line per day' invariant.
DAY_WINDOW_END_HOUR = 22
LATE_NIGHT_PROB = 0.23          # per line-day, on the last batch => ~5% of batches
RUNNING_FRACTION = 0.01
ABORTED_FRACTION = 0.02
# Your spec asked for 'mean ~1.5' AND '~1,500-2,500 alarm rows'. With ~790
# batches those pull in opposite directions (1.5 lands at ~1,270 rows), so the
# mean is set to satisfy the stated ROW COUNT, which is the checkable one.
# Drop it to 1.5 if you would rather have the distribution exact.
ALARMS_PER_BATCH_MEAN = 1.85
ALARMS_PER_BATCH_MAX = 5
STANDALONE_ALARM_FRACTION = 0.10
LONG_ALARM_PROB = 0.08


# --------------------------------------------------------------------------- #
# Row containers
# --------------------------------------------------------------------------- #

@dataclass
class Dataset:
    lines: list[tuple] = field(default_factory=list)
    batches: list[tuple] = field(default_factory=list)
    part_counts: list[tuple] = field(default_factory=list)
    alarms: list[tuple] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.lines)} lines, {len(self.batches)} batches, "
            f"{len(self.part_counts)} part_count rows, {len(self.alarms)} alarms"
        )


def _poisson(rng: random.Random, mean: float) -> int:
    """Knuth's sampler. Avoids a numpy dependency for one distribution."""
    limit = math.exp(-mean)
    k, p = 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _weighted_choice(rng: random.Random, weights: dict[str, float]) -> str:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def _split_rejects(rng: random.Random, total_rejects: int) -> tuple[int, int, int]:
    """Split rejects across the three categories, summing EXACTLY to the total."""
    if total_rejects <= 0:
        return (0, 0, 0)
    dimensional = int(round(total_rejects * REJECT_WEIGHTS[0] * rng.uniform(0.85, 1.15)))
    dimensional = max(0, min(dimensional, total_rejects))
    remaining = total_rejects - dimensional
    visual = int(round(remaining * 0.66 * rng.uniform(0.85, 1.15)))
    visual = max(0, min(visual, remaining))
    other = remaining - visual
    return dimensional, visual, other


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #

def generate_dataset(
    anchor: date | None = None,
    days: int | None = None,
    seed: int | None = None,
) -> Dataset:
    """Build the whole dataset in memory. Pure: same inputs => same rows."""
    anchor = anchor or settings.seed_anchor_date()
    days = days if days is not None else settings.seed_days
    rng = random.Random(seed if seed is not None else settings.seed_random_seed)

    data = Dataset(lines=[(lid, name, loc) for lid, name, loc in LINES])

    # ---- batches ---------------------------------------------------------
    raw: list[dict] = []
    first_day = anchor - timedelta(days=days - 1)

    for day_offset in range(days):
        current_day = first_day + timedelta(days=day_offset)
        for line_id, _, _ in LINES:
            count = rng.randint(*BATCHES_PER_LINE_PER_DAY)
            cursor = datetime(
                current_day.year, current_day.month, current_day.day, SHIFT_START_HOUR
            ) + timedelta(minutes=rng.randint(0, 45))
            window_end = datetime(
                current_day.year, current_day.month, current_day.day, DAY_WINDOW_END_HOUR
            )
            durations, gaps = _fit_day(rng, count, cursor, window_end)

            for index in range(count):
                is_last = index == count - 1
                # ~5% of all batches: start late evening, finish after midnight.
                # These exist specifically to break naive date filtering.
                if is_last and rng.random() < LATE_NIGHT_PROB:
                    start = datetime(
                        current_day.year, current_day.month, current_day.day, 22
                    ) + timedelta(minutes=rng.randint(0, 90))
                    hours = rng.uniform(2.0, 4.0)
                else:
                    start = cursor
                    hours = durations[index]

                product = rng.choice(list(PRODUCTS))
                target = int(round(PRODUCTS[product] * hours / 25.0) * 25)

                raw.append({
                    "line_id": line_id,
                    "product_code": product,
                    "start_time": start,
                    "planned_hours": hours,
                    "target_quantity": max(50, target),
                })

                cursor = start + timedelta(hours=hours)
                if index < len(gaps):
                    cursor += timedelta(hours=gaps[index])

    # Sort first, number second, so batch_id order is chronological order.
    raw.sort(key=lambda b: b["start_time"])
    for counter, batch in enumerate(raw, start=1):
        batch["batch_id"] = f"B-{batch['start_time'].year}-{counter:06d}"

    # ---- status: running (most recent ~1%), aborted (~2%) ----------------
    running_count = max(1, int(round(len(raw) * RUNNING_FRACTION)))
    running_ids = {b["batch_id"] for b in raw[-running_count:]}

    eligible = [b for b in raw if b["batch_id"] not in running_ids]
    aborted_count = max(1, int(round(len(raw) * ABORTED_FRACTION)))
    aborted_ids = {b["batch_id"] for b in rng.sample(eligible, min(aborted_count, len(eligible)))}

    now = datetime(anchor.year, anchor.month, anchor.day, 23, 59, 59)

    for batch in raw:
        bid = batch["batch_id"]
        if bid in running_ids:
            batch["status"] = "running"
            batch["end_time"] = None
            # A running batch is part-way through: used to scale its output.
            batch["progress"] = rng.uniform(0.2, 0.8)
        elif bid in aborted_ids:
            # Aborted batches stop early, so they run shorter and make less.
            batch["status"] = "aborted"
            actual = batch["planned_hours"] * rng.uniform(0.15, 0.55)
            batch["end_time"] = batch["start_time"] + timedelta(hours=actual)
            batch["progress"] = actual / batch["planned_hours"]
        else:
            batch["status"] = "completed"
            actual = batch["planned_hours"] * rng.uniform(0.95, 1.08)
            batch["end_time"] = batch["start_time"] + timedelta(hours=actual)
            batch["progress"] = 1.0

    # ---- part_counts: four tidy rows per batch ---------------------------
    for batch in raw:
        target = batch["target_quantity"]
        if batch["status"] == "completed":
            # Sometimes over, more often a little under.
            total = int(round(target * rng.uniform(0.93, 1.04)))
        elif batch["status"] == "aborted":
            total = int(round(target * batch["progress"] * rng.uniform(0.75, 1.0)))
        else:  # running
            total = int(round(target * batch["progress"]))
        total = max(0, total)

        good = int(round(total * rng.uniform(0.90, 0.98)))
        good = max(0, min(good, total))
        dimensional, visual, other = _split_rejects(rng, total - good)

        for category, value in (
            ("good", good),
            ("reject_dimensional", dimensional),
            ("reject_visual", visual),
            ("reject_other", other),
        ):
            data.part_counts.append((batch["batch_id"], category, value))

    # ---- alarms ----------------------------------------------------------
    for batch in raw:
        end = batch["end_time"] or min(
            now, batch["start_time"] + timedelta(hours=batch["planned_hours"] * batch["progress"])
        )
        window = max((end - batch["start_time"]).total_seconds(), 60.0)
        count = min(_poisson(rng, ALARMS_PER_BATCH_MEAN), ALARMS_PER_BATCH_MAX)

        for _ in range(count):
            code = _weighted_choice(rng, ALARM_WEIGHTS)
            description, severity = ALARM_CATALOGUE[code]
            start = batch["start_time"] + timedelta(seconds=rng.uniform(0, window))

            # ~1% of alarms overall: still active. Only ever on a running batch,
            # so the data stays self-consistent.
            if batch["status"] == "running" and rng.random() < 0.8:
                alarm_end = None
            else:
                alarm_end = start + timedelta(seconds=_alarm_seconds(rng))

            data.alarms.append((
                batch["line_id"], batch["batch_id"], code, description,
                severity, start, alarm_end,
            ))

    # ---- standalone alarms: no batch running ----------------------------
    by_line: dict[int, list[dict]] = {}
    for batch in raw:
        by_line.setdefault(batch["line_id"], []).append(batch)

    standalone_target = int(round(len(data.alarms) * STANDALONE_ALARM_FRACTION))
    for _ in range(standalone_target):
        line_id = rng.choice(list(by_line))
        sequence = by_line[line_id]
        index = rng.randrange(0, max(1, len(sequence) - 1))
        earlier, later = sequence[index], sequence[min(index + 1, len(sequence) - 1)]

        gap_start = earlier["end_time"] or earlier["start_time"] + timedelta(hours=1)
        gap_end = later["start_time"]
        if gap_end <= gap_start:
            # Batches abut with no real gap; drop the alarm in just afterwards.
            gap_start, gap_end = gap_start, gap_start + timedelta(minutes=20)

        start = gap_start + timedelta(
            seconds=rng.uniform(0, max((gap_end - gap_start).total_seconds(), 60.0))
        )
        code = _weighted_choice(rng, ALARM_WEIGHTS)
        description, severity = ALARM_CATALOGUE[code]
        data.alarms.append((
            line_id, None, code, description, severity,
            start, start + timedelta(seconds=_alarm_seconds(rng)),
        ))

    data.alarms.sort(key=lambda a: a[5])

    data.batches = [
        (b["batch_id"], b["line_id"], b["product_code"], b["start_time"],
         b["end_time"], b["status"], b["target_quantity"])
        for b in raw
    ]
    return data


def _fit_day(
    rng: random.Random, count: int, day_start: datetime, window_end: datetime
) -> tuple[list[float], list[float]]:
    """Sample `count` batch durations and the gaps between them, fitted to the day.

    Six batches of up to four hours plus generous gaps do not fit between 06:00
    and 22:00. Rather than let the schedule spill into tomorrow — which would
    corrupt the per-day counts — squeeze the gaps down to MIN_GAP_MINUTES first,
    and only then shorten the batches themselves.
    """
    durations = [rng.uniform(*BATCH_HOURS) for _ in range(count)]
    gaps = [rng.randint(*GAP_MINUTES) / 60.0 for _ in range(max(0, count - 1))]

    available = (window_end - day_start).total_seconds() / 3600.0
    overflow = sum(durations) + sum(gaps) - available
    if overflow <= 0:
        return durations, gaps

    # 1. take it out of the gaps, down to the floor
    floor = MIN_GAP_MINUTES / 60.0 * len(gaps)
    slack = sum(gaps) - floor
    if slack > 0:
        taken = min(overflow, slack)
        gaps = [g * (sum(gaps) - taken) / sum(gaps) for g in gaps]
        overflow -= taken

    # 2. still too long: scale the batches down proportionally
    if overflow > 0:
        total = sum(durations)
        durations = [max(MIN_BATCH_HOURS, d * (total - overflow) / total) for d in durations]

    return durations, gaps


def _alarm_seconds(rng: random.Random) -> float:
    """Mostly 30s-20min, with an occasional ~45min outage."""
    if rng.random() < LONG_ALARM_PROB:
        return rng.uniform(20 * 60, 45 * 60)
    return rng.uniform(30, 20 * 60)


# --------------------------------------------------------------------------- #
# Insertion
# --------------------------------------------------------------------------- #

def _batches_are_empty(cur, dialect: Dialect) -> bool:
    cur.execute(f"SELECT COUNT(*) FROM {dialect.quote_identifier('batches')}")
    (count,) = cur.fetchone()
    return count == 0


def _insert(cur, dialect: Dialect, table: str, columns: list[str], rows: list[tuple]) -> None:
    """Bulk INSERT via the driver.

    Uses `driver_placeholder`, NOT `placeholder`: this statement is executed
    straight against the driver and never passes through sql_guard, so there is
    no to_driver_sql() step to rewrite the neutral '?' that the templates author
    with. psycopg wants '%s'; pyodbc wants '?'.
    """
    if not rows:
        return
    marks = ", ".join([dialect.driver_placeholder] * len(columns))
    cols = ", ".join(dialect.quote_identifier(c) for c in columns)
    sql = f"INSERT INTO {dialect.quote_identifier(table)} ({cols}) VALUES ({marks})"
    cur.executemany(sql, rows)
    log.info("inserted %d rows into %s", len(rows), table)


def main() -> int:
    dialect = get_dialect()
    settings.require_db()

    log.info("dialect=%s anchor=%s days=%d rng_seed=%d",
             dialect.name, settings.seed_anchor_date(),
             settings.seed_days, settings.seed_random_seed)

    with admin_connection(dialect) as conn:
        with conn.cursor() as cur:
            # 1. schema (CREATE TABLE IF NOT EXISTS — safe to repeat)
            for statement in dialect.create_schema_statements():
                cur.execute(statement)
            conn.commit()
            log.info("schema ready: %s", ", ".join(TABLES))

            # 2. data, only when there is none
            if _batches_are_empty(cur, dialect):
                data = generate_dataset()
                log.info("generated %s", data.summary())

                _insert(cur, dialect, "lines",
                        ["line_id", "line_name", "location"], data.lines)
                _insert(cur, dialect, "batches",
                        ["batch_id", "line_id", "product_code", "start_time",
                         "end_time", "status", "target_quantity"], data.batches)
                _insert(cur, dialect, "part_counts",
                        ["batch_id", "category", "count"], data.part_counts)
                _insert(cur, dialect, "alarms",
                        ["line_id", "batch_id", "alarm_code", "description",
                         "severity", "start_time", "end_time"], data.alarms)
                conn.commit()
                log.info("seed complete.")
            else:
                log.info("batches already has rows — skipping data generation "
                         "(this is the idempotent path).")

            # 3. the readonly role, ALWAYS.
            #
            # db/init/ creates it, but Postgres runs init scripts only against an
            # EMPTY data volume. On a volume that already existed the role would
            # never appear, and `docker compose up` would fail on a fresh clone
            # for a reason that has nothing to do with the clone. Doing it here
            # too — idempotently, with admin rights, on every bring-up — makes
            # the stack self-healing and keeps the password in step with .env.
            readonly_user = settings.readonly_user
            existed = dialect.ensure_readonly_role(
                conn, readonly_user, settings.readonly_password, settings.db_name
            )
            conn.commit()
            log.info("readonly role '%s' %s", readonly_user,
                     "verified (password re-applied from .env)" if existed else "CREATED")

            # 4. grants, ALWAYS — a re-run repairs a missing GRANT.
            try:
                for statement in dialect.grant_readonly_statements(readonly_user, TABLES):
                    cur.execute(statement)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                raise RuntimeError(
                    f"could not grant SELECT to '{readonly_user}': {exc}\n"
                    f"\n"
                    f"The role itself was created or verified a moment earlier, so this\n"
                    f"is most likely a permissions problem with the ADMIN role: it must\n"
                    f"own the tables it is granting on. Check POSTGRES_ADMIN_USER in\n"
                    f".env against the owner reported by:\n"
                    f"\n"
                    f"    docker compose exec db psql -U {settings.admin_user} "
                    f"-d {settings.db_name} -c '\\dt'\n"
                    f"\n"
                    f"Starting clean also resolves it (database only — note that\n"
                    f"`down -v` would also delete the model volume):\n"
                    f"    docker compose down && docker volume rm mfg_pgdata"
                ) from exc
            log.info("granted SELECT on %s to '%s'", ", ".join(TABLES), readonly_user)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        log.error("SEED FAILED: %s: %s", type(exc).__name__, exc)
        log.error("The app will not start until this succeeds. "
                  "Check `docker compose logs db` and the *_DATABASE_URL values in .env.")
        sys.exit(1)
