"""Pipeline routing and failure envelopes, with the database stubbed out."""

from datetime import datetime

import pytest

import llm_query
import pipeline
import sql_guard
from db.connection import QueryExecutionError, QueryResult

NOW = datetime(2026, 8, 24, 14, 30)


@pytest.fixture
def fake_db(monkeypatch):
    """Capture what would have been executed, and return a canned result."""
    captured = {}

    def fake_run_select(sql, params=(), dialect=None):
        captured["sql"] = sql
        captured["params"] = list(params)
        return QueryResult(
            columns=["good_parts", "bad_parts", "total_parts", "yield_pct"],
            rows=[(9500, 500, 10000, 95.0)],
            execution_ms=2.5, sql=sql,
        )

    monkeypatch.setattr(pipeline, "run_select", fake_run_select)
    return captured


def test_template_route_never_calls_the_model(fake_db, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("the template path must not call the LLM")

    monkeypatch.setattr(llm_query, "generate_sql", explode)
    monkeypatch.setattr(pipeline.llm_query, "generate_sql", explode)

    answer = pipeline.answer_question("How many good vs bad parts on Line A last week?")
    assert answer.ok
    assert answer.route == "template"
    assert answer.template_id == "good_vs_bad"
    assert answer.llm_ms is None
    assert "9,500 good parts" in answer.answer


def test_template_route_binds_entities_as_parameters(fake_db):
    pipeline.answer_question("good vs bad parts on Line A last week")
    assert "Line A" in fake_db["params"]
    assert "Line A" not in fake_db["sql"]


def test_llm_route_is_used_when_no_template_matches(fake_db, monkeypatch):
    monkeypatch.setattr(pipeline.llm_query, "generate_sql", lambda q, d=None: llm_query.LlmSql(
        sql="SELECT product_code FROM batches", raw_response="...",
        duration_ms=1234.0, model="qwen2.5-coder:7b",
    ))
    answer = pipeline.answer_question("which product had the worst reject rate?")
    assert answer.route == "llm"
    assert answer.llm_ms == 1234.0
    assert answer.model == "qwen2.5-coder:7b"


def test_unsafe_llm_sql_is_rejected_not_repaired(fake_db, monkeypatch):
    monkeypatch.setattr(pipeline.llm_query, "generate_sql", lambda q, d=None: llm_query.LlmSql(
        sql="DROP TABLE batches", raw_response="DROP TABLE batches",
        duration_ms=10.0, model="m",
    ))
    answer = pipeline.answer_question("delete everything please")
    assert answer.ok is False
    assert answer.route == "rejected"
    assert answer.answer.startswith(pipeline.REJECTION_MESSAGE)
    # The offending SQL is shown, never silently rewritten into something valid.
    assert answer.sql == "DROP TABLE batches"


def test_model_outage_is_reported_clearly(monkeypatch):
    def unavailable(q, d=None):
        raise llm_query.LlmUnavailable("could not reach Ollama at http://ollama:11434")

    monkeypatch.setattr(pipeline.llm_query, "generate_sql", unavailable)
    answer = pipeline.answer_question("something no template handles at all zzz")
    assert answer.ok is False
    assert answer.route == "error"
    assert "ollama" in answer.detail.lower()


def test_database_error_is_reported_clearly(monkeypatch):
    def boom(sql, params=(), dialect=None):
        raise QueryExecutionError("canceling statement due to statement timeout")

    monkeypatch.setattr(pipeline, "run_select", boom)
    answer = pipeline.answer_question("How many good vs bad parts last week?")
    assert answer.ok is False
    assert answer.route == "error"
    assert "timeout" in answer.detail


def test_row_cap_is_always_present_on_executed_sql(fake_db):
    pipeline.answer_question("How many good vs bad parts last week?")
    assert "LIMIT" in fake_db["sql"].upper()


def test_empty_question_is_handled(fake_db):
    answer = pipeline.answer_question("   ")
    assert answer.ok is False
    assert answer.route == "rejected"


def test_decimal_and_datetime_are_json_safe():
    from decimal import Decimal
    assert pipeline._jsonable(Decimal("99.35")) == "99.35"
    assert pipeline._jsonable(datetime(2026, 7, 14, 9, 30)) == "2026-07-14 09:30:00"
