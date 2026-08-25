"""The deterministic query path: 11 hand-written question templates.

If a question matches one of these it never reaches the LLM — the answer is
exact, instant, and identical every time. That is what makes the demo reliable;
the model is the fallback, not the primary.

Every template returns parameterised SQL. Extracted entities become bound
parameters, never string-concatenated fragments. The only dialect-specific SQL
here comes from `dialect.duration_seconds()` and `dialect.truncate_to_day()`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import entities
from db.dialect import Dialect, get_dialect


@dataclass
class TemplateAnswer:
    """A matched template, ready for the guard."""

    template_id: str
    sql: str
    params: list[Any]
    title: str
    #: Top-N limit the question asked for. Applied via the dialect, so it comes
    #: out as LIMIT on Postgres and TOP on SQL Server.
    row_limit: int | None = None
    context: dict[str, Any] = field(default_factory=dict)


class _Where:
    """Accumulates WHERE fragments and their bound parameters, in order."""

    def __init__(self, placeholder: str) -> None:
        self._placeholder = placeholder
        self.clauses: list[str] = []
        self.params: list[Any] = []

    def add(self, clause_template: str, *params: Any) -> None:
        self.clauses.append(clause_template.format(p=self._placeholder))
        self.params.extend(params)

    def date_range(self, column: str, rng: entities.DateRange) -> None:
        self.add(f"{column} >= {{p}} AND {column} < {{p}}", rng.start, rng.end)

    def line(self, line_name: str | None) -> None:
        if line_name:
            self.add("l.line_name = {p}", line_name)

    def render(self, indent: str = "  ") -> str:
        return f"\nWHERE {(chr(10) + indent + 'AND ').join(self.clauses)}"


def _search(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


# --------------------------------------------------------------------------- #
# Scope guards
#
# A template that does not group by the dimension the question asks for MUST
# decline. Answering "worst reject rate by PRODUCT" with a per-LINE breakdown is
# worse than not answering: it is confidently wrong, and the demo audience
# cannot tell. Declining sends the question to the LLM, which can group freely.
# --------------------------------------------------------------------------- #

_DIMENSIONS = {
    "product": r"\b(?:per|each|by|for each|for every)\s+product\w*\b"
               r"|\bproduct[\s_-]?codes?\b|\bprd[\s-]?10[0-5]\b",
    "line": r"\b(?:per|each|by|for each|for every)\s+lines?\b|\beach line'?s?\b",
    "day": r"\b(?:daily|per day|by day|each day|day by day|day-by-day)\b",
    # Day OF THE WEEK is a different grouping from calendar day, and no
    # template implements it — daily_production truncates to a date.
    "weekday": r"\bday of (?:the )?week\b|\bweekday\b|\bwhich day\b",
    "category": r"\b(?:per|each|by)\s+categor\w+\b",
    "batch": r"\b(?:per|each|by)\s+batch\w*\b",
    "severity": r"\b(?:per|each|by)\s+severit\w+\b",
    "product_alt": r"\bwhich product\b|\bwhat product\b",
}


def _requested_dimensions(question: str) -> set[str]:
    found = {
        name for name, pattern in _DIMENSIONS.items()
        if re.search(pattern, question or "", re.IGNORECASE)
    }
    # 'which product' is the same ask as 'by product'.
    if "product_alt" in found:
        found.discard("product_alt")
        found.add("product")
    return found


def _wrong_grouping(question: str, supported: set[str]) -> bool:
    """True when the question asks to break results down a way this SQL cannot."""
    return bool(_requested_dimensions(question) - supported)


#: Alarm asks that none of the three alarm templates actually implement.
_ALARM_OUT_OF_SCOPE = (
    r"\b(average|avg|mean)\b",                      # templates count and sum, never average
    r"\bper occurrence\b|\beach occurrence\b",
    r"\blongest\b",                                 # wants individual alarms, not code totals
    r"\bno batch\b|\bbetween batches\b|\bstandalone\b|\bnot tied to a batch\b",
    r"\bno alarms?\b|\bwithout (?:any )?alarms?\b",
)


def _alarm_out_of_scope(question: str) -> bool:
    return _search(_ALARM_OUT_OF_SCOPE, question)


# --------------------------------------------------------------------------- #
# Shared FROM clauses
# --------------------------------------------------------------------------- #

_PARTS_FROM = """FROM part_counts pc
JOIN batches b ON b.batch_id = pc.batch_id
JOIN lines   l ON l.line_id  = b.line_id"""

_BATCH_FROM = """FROM batches b
JOIN lines l ON l.line_id = b.line_id"""

_ALARM_FROM = """FROM alarms a
JOIN lines l ON l.line_id = a.line_id"""

_GOOD = "SUM(CASE WHEN pc.category = 'good' THEN pc.\"count\" ELSE 0 END)"
_BAD = "SUM(CASE WHEN pc.category <> 'good' THEN pc.\"count\" ELSE 0 END)"
_TOTAL = 'SUM(pc."count")'


# --------------------------------------------------------------------------- #
# 1. good vs bad parts
# --------------------------------------------------------------------------- #
def _good_vs_bad(q: str, now: datetime, d: Dialect) -> TemplateAnswer | None:
    if not _search(
        (
            r"\bgood\b.*\b(bad|reject|scrap|defect)",
            r"\b(bad|reject|scrap|defect)\w*\b.*\bgood\b",
            r"how many good\b",
            r"\bgood parts\b",
            r"\bquality (split|breakdown)\b",
        ),
        q,
    ):
        return None
    # Returns a single total row — no breakdown of any kind.
    if _wrong_grouping(q, set()):
        return None

    rng = entities.extract_date_range(q, now)
    line = entities.extract_line(q)
    where = _Where(d.placeholder)
    where.date_range("b.start_time", rng)
    where.line(line)

    sql = f"""SELECT {_GOOD} AS good_parts,
       {_BAD} AS bad_parts,
       {_TOTAL} AS total_parts,
       ROUND(100.0 * {_GOOD} / NULLIF({_TOTAL}, 0), 2) AS yield_pct
{_PARTS_FROM}{where.render()}"""

    return TemplateAnswer(
        "good_vs_bad", sql, where.params,
        f"Good vs bad parts{_for_line(line)} over {rng.label}",
        context={"line": line, "period": rng.label},
    )


# --------------------------------------------------------------------------- #
# 2. reject breakdown by category
# --------------------------------------------------------------------------- #
def _reject_breakdown(q: str, now: datetime, d: Dialect) -> TemplateAnswer | None:
    if not _search(
        (
            r"\b(reject|scrap|defect)\w*\b.*\b(breakdown|by category|per category|split|categor)",
            r"\b(breakdown|split|categor\w+)\b.*\b(reject|scrap|defect)",
            r"\breject reasons?\b",
            r"\bwhy .*(reject|scrap)",
        ),
        q,
    ):
        return None
    if _wrong_grouping(q, {"category"}):
        return None

    rng = entities.extract_date_range(q, now)
    line = entities.extract_line(q)
    where = _Where(d.placeholder)
    where.add("pc.category <> 'good'")
    where.date_range("b.start_time", rng)
    where.line(line)

    sql = f"""SELECT pc.category,
       {_TOTAL} AS parts
{_PARTS_FROM}{where.render()}
GROUP BY pc.category
ORDER BY parts DESC"""

    return TemplateAnswer(
        "reject_breakdown", sql, where.params,
        f"Reject breakdown by category{_for_line(line)} over {rng.label}",
        context={"line": line, "period": rng.label},
    )


# --------------------------------------------------------------------------- #
# 3. top alarms by frequency
# --------------------------------------------------------------------------- #
def _top_alarms_frequency(q: str, now: datetime, d: Dialect) -> TemplateAnswer | None:
    if not re.search(r"\balarm|\bfault|\bstoppage", q, re.IGNORECASE):
        return None
    if _mentions_downtime(q):
        return None  # template 4 owns that question
    if not _search(
        (
            r"\b(top|most (common|frequent)|commonest|frequent|often|worst)\b",
            r"\bhow many alarms\b",
            r"\balarm (count|frequency|counts)\b",
            r"\bwhich alarms?\b",
        ),
        q,
    ):
        return None
    if _alarm_out_of_scope(q) or _wrong_grouping(q, {"severity"}):
        return None

    rng = entities.extract_date_range(q, now)
    line = entities.extract_line(q)
    severity = entities.extract_severity(q)
    top_n = entities.extract_top_n(q, default=5)

    where = _Where(d.placeholder)
    where.date_range("a.start_time", rng)
    where.line(line)
    if severity:
        where.add("a.severity = {p}", severity)

    sql = f"""SELECT a.alarm_code,
       a.description,
       a.severity,
       COUNT(*) AS occurrences
{_ALARM_FROM}{where.render()}
GROUP BY a.alarm_code, a.description, a.severity
ORDER BY occurrences DESC, a.alarm_code"""

    return TemplateAnswer(
        "top_alarms_frequency", sql, where.params,
        f"Top {top_n} alarms by frequency{_for_line(line)} over {rng.label}",
        row_limit=top_n,
        context={"line": line, "period": rng.label, "top_n": top_n, "severity": severity},
    )


# --------------------------------------------------------------------------- #
# 4. top alarms by total downtime
# --------------------------------------------------------------------------- #
def _top_alarms_downtime(q: str, now: datetime, d: Dialect) -> TemplateAnswer | None:
    if not re.search(r"\balarm|\bfault|\bstoppage|\bdowntime", q, re.IGNORECASE):
        return None
    if not _mentions_downtime(q):
        return None
    if _alarm_out_of_scope(q) or _wrong_grouping(q, {"severity"}):
        return None

    rng = entities.extract_date_range(q, now)
    line = entities.extract_line(q)
    severity = entities.extract_severity(q)
    top_n = entities.extract_top_n(q, default=5)

    where = _Where(d.placeholder)
    where.add("a.end_time IS NOT NULL")
    where.date_range("a.start_time", rng)
    where.line(line)
    if severity:
        where.add("a.severity = {p}", severity)

    duration = d.duration_seconds("a.start_time", "a.end_time")
    sql = f"""SELECT a.alarm_code,
       a.description,
       a.severity,
       COUNT(*) AS occurrences,
       ROUND(SUM({duration}) / 60.0, 1) AS downtime_minutes
{_ALARM_FROM}{where.render()}
GROUP BY a.alarm_code, a.description, a.severity
ORDER BY downtime_minutes DESC, a.alarm_code"""

    return TemplateAnswer(
        "top_alarms_downtime", sql, where.params,
        f"Top {top_n} alarms by total downtime{_for_line(line)} over {rng.label}",
        row_limit=top_n,
        context={"line": line, "period": rng.label, "top_n": top_n, "severity": severity},
    )


# --------------------------------------------------------------------------- #
# 5. batch count
# --------------------------------------------------------------------------- #
def _batch_count(q: str, now: datetime, d: Dialect) -> TemplateAnswer | None:
    if not _search(
        (
            r"\bhow many batch\w*\b",
            r"\bnumber of batch\w*\b",
            r"\bbatch count\b",
            r"\b(batches|runs) (did|were|has|have|was)\b",
            r"\bcount .*\bbatch\w*\b",
        ),
        q,
    ):
        return None
    if _wrong_grouping(q, {"line"}):
        return None

    rng = entities.extract_date_range(q, now)
    line = entities.extract_line(q)
    where = _Where(d.placeholder)
    where.date_range("b.start_time", rng)
    where.line(line)

    sql = f"""SELECT l.line_name,
       b.status,
       COUNT(*) AS batches
{_BATCH_FROM}{where.render()}
GROUP BY l.line_name, b.status
ORDER BY l.line_name, b.status"""

    return TemplateAnswer(
        "batch_count", sql, where.params,
        f"Batches run{_for_line(line)} over {rng.label}",
        context={"line": line, "period": rng.label},
    )


# --------------------------------------------------------------------------- #
# 6. total / average parts produced
# --------------------------------------------------------------------------- #
def _production_totals(q: str, now: datetime, d: Dialect) -> TemplateAnswer | None:
    if not _search(
        (
            r"\b(total|average|avg|mean)\b.*\b(parts|units|produced|production|output|quantity)",
            r"\b(parts|units|output|production)\b.*\b(total|average|avg|mean)\b",
            r"\bhow many (parts|units)\b",
            r"\bhow much did we (produce|make)\b",
            r"\b(produced|output)\b.*\b(in|on|during|last|this|yesterday|today)\b",
        ),
        q,
    ):
        return None
    if _wrong_grouping(q, {"batch"}):
        return None

    rng = entities.extract_date_range(q, now)
    line = entities.extract_line(q)
    where = _Where(d.placeholder)
    where.date_range("b.start_time", rng)
    where.line(line)

    sql = f"""SELECT COUNT(*) AS batches,
       SUM(t.total_parts) AS total_parts,
       SUM(t.good_parts) AS good_parts,
       ROUND(AVG(t.total_parts), 1) AS avg_parts_per_batch
FROM (
  SELECT b.batch_id,
         {_TOTAL} AS total_parts,
         {_GOOD} AS good_parts
  {_PARTS_FROM}{where.render(indent='    ')}
  GROUP BY b.batch_id
) t"""

    return TemplateAnswer(
        "production_totals", sql, where.params,
        f"Production totals{_for_line(line)} over {rng.label}",
        context={"line": line, "period": rng.label},
    )


# --------------------------------------------------------------------------- #
# 7. yield per line
# --------------------------------------------------------------------------- #
def _yield_by_line(q: str, now: datetime, d: Dialect) -> TemplateAnswer | None:
    if not _search(
        (
            r"\byield\b",
            r"\b(scrap|reject|defect) rate\b",
            r"\bquality\b.*\b(per|each|by) line\b",
            r"\bcompare\b.*\blines\b",
            r"\bwhich line\b.*\b(best|worst|most|least|highest|lowest)\b",
        ),
        q,
    ):
        return None
    # This SQL has no notion of target attainment — see worst_batches / the LLM.
    if _wrong_grouping(q, {"line"}) or re.search(r"\btarget\b", q, re.IGNORECASE):
        return None

    rng = entities.extract_date_range(q, now)
    line = entities.extract_line(q)
    where = _Where(d.placeholder)
    where.date_range("b.start_time", rng)
    where.line(line)

    sql = f"""SELECT l.line_name,
       {_GOOD} AS good_parts,
       {_BAD} AS bad_parts,
       {_TOTAL} AS total_parts,
       ROUND(100.0 * {_GOOD} / NULLIF({_TOTAL}, 0), 2) AS yield_pct
{_PARTS_FROM}{where.render()}
GROUP BY l.line_name
ORDER BY yield_pct DESC"""

    return TemplateAnswer(
        "yield_by_line", sql, where.params,
        f"Yield by line over {rng.label}",
        context={"line": line, "period": rng.label},
    )


# --------------------------------------------------------------------------- #
# 8. batch status summary
# --------------------------------------------------------------------------- #
_STATUS_WORDS = {
    "aborted": "aborted", "abort": "aborted", "cancelled": "aborted",
    "failed": "aborted", "scrapped": "aborted",
    "running": "running", "in progress": "running", "ongoing": "running",
    "active": "running", "still going": "running", "currently": "running",
    "completed": "completed", "finished": "completed", "complete": "completed",
}


def _batch_status_summary(q: str, now: datetime, d: Dialect) -> TemplateAnswer | None:
    text = (q or "").lower()
    status = next((v for k, v in _STATUS_WORDS.items() if re.search(rf"\b{k}\b", text)), None)
    if not status or not re.search(r"\bbatch\w*\b|\brun\w*\b", text):
        return None
    # "How many alarms fired when no batch was running?" contains a status word
    # but is an ALARM question. The subject wins over the keyword.
    if re.search(r"\balarm|\bfault\b|\bstoppage", text):
        return None
    if _wrong_grouping(q, set()):
        return None

    rng = entities.extract_date_range(q, now)
    line = entities.extract_line(q)
    where = _Where(d.placeholder)
    where.add("b.status = {p}", status)
    # 'running' batches are by definition current, so a date filter would only
    # confuse the answer. Everything else gets the requested period.
    if status != "running":
        where.date_range("b.start_time", rng)
    where.line(line)

    sql = f"""SELECT b.batch_id,
       l.line_name,
       b.product_code,
       b.start_time,
       b.end_time,
       b.target_quantity
{_BATCH_FROM}{where.render()}
ORDER BY b.start_time DESC"""

    period = "right now" if status == "running" else rng.label
    return TemplateAnswer(
        "batch_status_summary", sql, where.params,
        f"{status.capitalize()} batches{_for_line(line)} — {period}",
        row_limit=entities.extract_top_n(q, default=50),
        context={"line": line, "period": period, "status": status},
    )


# --------------------------------------------------------------------------- #
# 9. daily production trend
# --------------------------------------------------------------------------- #
def _daily_production(q: str, now: datetime, d: Dialect) -> TemplateAnswer | None:
    if not _search(
        (
            r"\b(daily|per day|by day|each day|day by day|day-by-day)\b",
            r"\b(trend|over time|breakdown by date|by date)\b",
        ),
        q,
    ):
        return None
    if _wrong_grouping(q, {"day", "line"}):
        return None

    rng = entities.extract_date_range(q, now)
    line = entities.extract_line(q)
    where = _Where(d.placeholder)
    where.date_range("b.start_time", rng)
    where.line(line)

    day = d.truncate_to_day("b.start_time")
    # The grouping expression is repeated rather than referenced by alias:
    # Postgres allows GROUP BY <alias>, T-SQL does not.
    sql = f"""SELECT {day} AS production_day,
       COUNT(DISTINCT b.batch_id) AS batches,
       {_TOTAL} AS total_parts,
       {_GOOD} AS good_parts
{_PARTS_FROM}{where.render()}
GROUP BY {day}
ORDER BY {day}"""

    return TemplateAnswer(
        "daily_production", sql, where.params,
        f"Daily production{_for_line(line)} over {rng.label}",
        context={"line": line, "period": rng.label},
    )


# --------------------------------------------------------------------------- #
# 10. alarm listing
# --------------------------------------------------------------------------- #
def _alarm_list(q: str, now: datetime, d: Dialect) -> TemplateAnswer | None:
    if not re.search(r"\balarm|\bfault\b|\bstoppage", q, re.IGNORECASE):
        return None
    if not _search(
        (
            r"\b(show|list|what|which|any|display|give me|tell me about)\b",
            r"\b(ongoing|active|open|unresolved|still)\b",
            r"\b(recent|latest|last)\b",
        ),
        q,
    ):
        return None
    if _alarm_out_of_scope(q) or _wrong_grouping(q, {"severity"}):
        return None

    rng = entities.extract_date_range(q, now)
    line = entities.extract_line(q)
    severity = entities.extract_severity(q)
    ongoing = bool(re.search(r"\b(ongoing|active|open|unresolved|still (active|on|going))\b",
                             q, re.IGNORECASE))

    where = _Where(d.placeholder)
    if ongoing:
        where.add("a.end_time IS NULL")
    else:
        where.date_range("a.start_time", rng)
    where.line(line)
    if severity:
        where.add("a.severity = {p}", severity)

    duration = d.duration_seconds("a.start_time", "a.end_time")
    sql = f"""SELECT a.start_time,
       l.line_name,
       a.alarm_code,
       a.description,
       a.severity,
       a.batch_id,
       ROUND({duration} / 60.0, 1) AS duration_minutes
{_ALARM_FROM}{where.render()}
ORDER BY a.start_time DESC"""

    what = "Ongoing alarms" if ongoing else f"{(severity or 'All').capitalize()} alarms"
    period = "right now" if ongoing else rng.label
    return TemplateAnswer(
        "alarm_list", sql, where.params,
        f"{what}{_for_line(line)} — {period}",
        row_limit=entities.extract_top_n(q, default=25),
        context={"line": line, "period": period, "severity": severity, "ongoing": ongoing},
    )


# --------------------------------------------------------------------------- #
# 11. batches that missed target
# --------------------------------------------------------------------------- #
def _worst_batches(q: str, now: datetime, d: Dialect) -> TemplateAnswer | None:
    if not _search(
        (
            r"\b(missed|miss|under|below|short of|behind)\b.*\btarget\b",
            r"\btarget\b.*\b(missed|miss|shortfall|under|below)\b",
            r"\bshortfall\b",
            r"\bworst (performing )?batch\w*\b",
            r"\bunderperform\w*\b",
        ),
        q,
    ):
        return None
    if _wrong_grouping(q, {"batch"}):
        return None

    rng = entities.extract_date_range(q, now)
    line = entities.extract_line(q)
    top_n = entities.extract_top_n(q, default=10)

    where = _Where(d.placeholder)
    where.add("b.status = 'completed'")
    where.date_range("b.start_time", rng)
    where.line(line)

    sql = f"""SELECT b.batch_id,
       l.line_name,
       b.product_code,
       b.target_quantity,
       {_TOTAL} AS actual_parts,
       b.target_quantity - {_TOTAL} AS shortfall
{_PARTS_FROM}{where.render()}
GROUP BY b.batch_id, l.line_name, b.product_code, b.target_quantity
ORDER BY shortfall DESC"""

    return TemplateAnswer(
        "worst_batches", sql, where.params,
        f"Batches furthest below target{_for_line(line)} over {rng.label}",
        row_limit=top_n,
        context={"line": line, "period": rng.label, "top_n": top_n},
    )


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

def _mentions_downtime(q: str) -> bool:
    return bool(re.search(r"\b(downtime|down time|duration|longest|time lost|lost time|"
                          r"minutes|hours|stopped for)\b", q, re.IGNORECASE))


def _for_line(line: str | None) -> str:
    return f" on {line}" if line else ""


#: Priority order. The first template that matches wins, so the more specific
#: patterns must come first.
TEMPLATES: tuple[tuple[str, Callable[..., TemplateAnswer | None]], ...] = (
    ("top_alarms_downtime", _top_alarms_downtime),
    ("top_alarms_frequency", _top_alarms_frequency),
    ("alarm_list", _alarm_list),
    ("reject_breakdown", _reject_breakdown),
    ("good_vs_bad", _good_vs_bad),
    ("yield_by_line", _yield_by_line),
    ("daily_production", _daily_production),
    ("worst_batches", _worst_batches),
    ("batch_status_summary", _batch_status_summary),
    ("batch_count", _batch_count),
    ("production_totals", _production_totals),
)


def match_template(
    question: str,
    now: datetime | None = None,
    dialect: Dialect | None = None,
) -> TemplateAnswer | None:
    """Return the first matching template, or None to fall through to the LLM."""
    if not (question or "").strip():
        return None
    dialect = dialect or get_dialect()
    now = now or datetime.now()

    for _, builder in TEMPLATES:
        answer = builder(question, now, dialect)
        if answer is not None:
            if answer.row_limit:
                answer.sql = dialect.with_limit(answer.sql, answer.row_limit)
            return answer
    return None


def template_ids() -> list[str]:
    return [name for name, _ in TEMPLATES]


__all__ = ["TemplateAnswer", "match_template", "template_ids", "TEMPLATES"]
