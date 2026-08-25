"""Every template must match its questions AND produce guard-valid SQL.

The second half matters most: a template that emits SQL the guard rejects would
fail only at demo time, on stage.
"""

from datetime import datetime

import pytest

import sql_guard
import templates
from db.dialect import get_dialect

NOW = datetime(2026, 8, 24, 14, 30)

# question -> the template that must claim it
EXPECTED = [
    ("How many good vs bad parts did Line A make last week?", "good_vs_bad"),
    ("good parts on line b yesterday", "good_vs_bad"),
    ("Reject breakdown by category for Line B last month", "reject_breakdown"),
    ("what are the reject reasons on Line C?", "reject_breakdown"),
    ("Top 5 alarms on Line C in the last 30 days", "top_alarms_frequency"),
    ("which alarms happen most often?", "top_alarms_frequency"),
    ("Which alarms caused the most downtime last month?", "top_alarms_downtime"),
    ("top 3 alarms by downtime on line a", "top_alarms_downtime"),
    ("How many batches did Line A run last week?", "batch_count"),
    ("number of batches in July", "batch_count"),
    ("Total parts produced yesterday", "production_totals"),
    ("average parts per batch in July", "production_totals"),
    ("What was the yield per line last month?", "yield_by_line"),
    ("which line had the worst scrap rate?", "yield_by_line"),
    ("How many batches were aborted last month?", "batch_status_summary"),
    ("are any batches still running?", "batch_status_summary"),
    ("Daily production for Line A over the last 14 days", "daily_production"),
    ("show me the production trend", "daily_production"),
    ("show critical alarms on Line B this week", "alarm_list"),
    ("any ongoing alarms?", "alarm_list"),
    ("Which batches missed target by the most last month?", "worst_batches"),
    ("worst performing batches on line c", "worst_batches"),
]


@pytest.mark.parametrize("question,template_id", EXPECTED)
def test_question_routes_to_the_right_template(question, template_id):
    match = templates.match_template(question, now=NOW)
    assert match is not None, f"no template matched: {question!r}"
    assert match.template_id == template_id


@pytest.mark.parametrize("question,template_id", EXPECTED)
def test_template_sql_passes_the_guard(question, template_id):
    """Substitute the placeholders, then validate exactly as the pipeline does."""
    match = templates.match_template(question, now=NOW)
    guarded = sql_guard.validate(match.sql)
    assert guarded.sql.upper().startswith("SELECT")
    assert "LIMIT" in guarded.sql.upper()


@pytest.mark.parametrize("question,_", EXPECTED)
def test_params_count_matches_placeholders(question, _):
    """A mismatch here is the bug that surfaces as a driver error at runtime."""
    dialect = get_dialect()
    match = templates.match_template(question, now=NOW)
    assert match.sql.count(dialect.placeholder) == len(match.params)


def test_entities_are_bound_parameters_never_inlined():
    """User text must never reach the SQL string itself."""
    match = templates.match_template("good vs bad parts on Line A last week", now=NOW)
    assert "Line A" not in match.sql
    assert "Line A" in match.params


@pytest.mark.parametrize("question", [
    "which product had the worst reject rate?",
    "what is the average batch duration by line?",
    "tell me a joke",
    # A template that cannot group the way the question asks must DECLINE.
    # Answering these with the wrong grouping would be confidently wrong.
    "what was the total good output per product code in July?",   # by product
    "how many critical alarms did each line have this month?",     # by line, not by code
    "which day of the week has the worst yield?",                  # by weekday
    "which alarm code causes the most downtime per occurrence?",   # average, not sum
    "show me the 5 longest alarms in the last 7 days",             # individual alarms
    "which batches last week had no alarms at all?",               # anti-join
    "how many alarms fired when no batch was running?",            # alarms, not batches
    "how does each line's yield compare against its target quantity?",
])
def test_unmatched_question_falls_through_to_the_llm(question):
    assert templates.match_template(question, now=NOW) is None


def test_top_n_becomes_a_row_limit():
    match = templates.match_template("top 3 alarms on line a", now=NOW)
    assert match.row_limit == 3
    assert "LIMIT 3" in match.sql.upper()


def test_every_template_is_reachable():
    """Each declared template id must be claimed by at least one test question."""
    covered = {tid for _, tid in EXPECTED}
    assert covered == set(templates.template_ids())


def test_running_batches_ignore_the_date_filter():
    """'running' is a now-state; a date window would only confuse the answer."""
    match = templates.match_template("are any batches still running?", now=NOW)
    assert match.context["status"] == "running"
    assert match.params == ["running"]
