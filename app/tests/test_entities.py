"""Entity extraction: dates are the part that silently gets demos wrong."""

from datetime import datetime

import pytest

import entities

NOW = datetime(2026, 8, 24, 14, 30)      # a Monday


def rng(question):
    return entities.extract_date_range(question, NOW)


def test_yesterday():
    r = rng("how many parts yesterday?")
    assert r.start == datetime(2026, 8, 23)
    assert r.end == datetime(2026, 8, 24)


def test_today():
    r = rng("what happened today?")
    assert r.start == datetime(2026, 8, 24)
    assert r.end == datetime(2026, 8, 25)


def test_last_week_is_the_previous_calendar_week():
    r = rng("output last week")
    assert r.start == datetime(2026, 8, 17)      # previous Monday
    assert r.end == datetime(2026, 8, 24)        # this Monday, exclusive


def test_last_month_is_the_previous_calendar_month():
    r = rng("rejects last month")
    assert r.start == datetime(2026, 7, 1)
    assert r.end == datetime(2026, 8, 1)
    assert r.label == "July 2026"


def test_last_n_days():
    r = rng("alarms in the last 14 days")
    assert r.start == datetime(2026, 8, 11)
    assert r.end == datetime(2026, 8, 25)


def test_named_month_in_the_past():
    r = rng("production in July")
    assert (r.start, r.end) == (datetime(2026, 7, 1), datetime(2026, 8, 1))


def test_named_month_not_yet_reached_means_last_year():
    r = rng("production in December")
    assert r.start.year == 2025


def test_explicit_iso_day():
    r = rng("what happened on 2026-07-14?")
    assert (r.start, r.end) == (datetime(2026, 7, 14), datetime(2026, 7, 15))


def test_between_two_dates():
    r = rng("batches between 2026-07-01 and 2026-07-10")
    assert r.start == datetime(2026, 7, 1)
    assert r.end == datetime(2026, 7, 11)        # end is exclusive


def test_default_is_last_30_days_and_is_marked_implicit():
    r = rng("how many good parts")
    assert r.explicit is False
    assert (r.end - r.start).days == 31


def test_range_is_half_open():
    """Exclusive end: the classic 'lost the last day' bug must not reappear."""
    r = rng("on 2026-07-14")
    assert r.end > r.start
    assert r.end.hour == 0 and r.end.minute == 0


@pytest.mark.parametrize("text,expected", [
    ("how many parts on Line A", "Line A"),
    ("line b rejects", "Line B"),
    ("LINE C alarms", "Line C"),
    ("what about Line-A", "Line A"),
    ("total output", None),
])
def test_line_extraction(text, expected):
    assert entities.extract_line(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("top 3 alarms", 3),
    ("top five alarms", 5),
    ("worst 10 batches", 10),
    ("alarms", 5),                # default
    ("top 9999 alarms", 50),      # clamped
])
def test_top_n(text, expected):
    assert entities.extract_top_n(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("critical alarms", "critical"),
    ("any warning alarms", "warning"),
    ("info alarms", "info"),
    ("all alarms", None),
])
def test_severity(text, expected):
    assert entities.extract_severity(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("dimensional rejects", "reject_dimensional"),
    ("visual defects", "reject_visual"),
    ("other rejects", "reject_other"),
    ("rejects", None),
])
def test_reject_category(text, expected):
    assert entities.extract_reject_category(text) == expected


def test_product_code():
    assert entities.extract_product("how did PRD-103 do?") == "PRD-103"
    assert entities.extract_product("prd 105 output") == "PRD-105"
    assert entities.extract_product("output") is None
