"""Pipeline routing and failure envelopes, with the database stubbed out."""

import time
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


# --------------------------------------------------------------------------- #
# Prompt-cache warm-up interaction
# --------------------------------------------------------------------------- #

def test_llm_path_does_not_wait_when_no_warm_up_is_running(fake_db, monkeypatch):
    """A bare process must not block for the full timeout.

    Waiting on a warm-up that was never started turns every LLM question into a
    three-minute hang instead of merely a slow answer.
    """
    from readiness import readiness

    readiness.llm_warm_started.clear()
    readiness.llm_warm.clear()
    monkeypatch.setattr(pipeline.llm_query, "generate_sql", lambda q, d=None: llm_query.LlmSql(
        sql="SELECT product_code FROM batches", raw_response="x",
        duration_ms=1.0, model="m",
    ))

    started = time.perf_counter()
    answer = pipeline.answer_question("which product had the worst reject rate?")
    assert answer.route == "llm"
    assert time.perf_counter() - started < 2.0


def test_llm_path_waits_while_a_warm_up_is_in_flight(monkeypatch):
    """Ollama is single-threaded; racing the warm-up is how requests time out."""
    from readiness import Readiness

    state = Readiness()
    state.llm_warm_started.set()          # warm-up running
    state.llm_warm.clear()

    started = time.perf_counter()
    state.wait_for_warm_model(timeout=0.3)
    waited = time.perf_counter() - started
    assert waited >= 0.25, "should have blocked until the warm-up finished"

    state.llm_warm.set()                  # warm-up done
    started = time.perf_counter()
    state.wait_for_warm_model(timeout=5.0)
    assert time.perf_counter() - started < 0.1, "should not block once warm"


def test_model_saying_it_cannot_answer_is_not_reported_as_a_safety_failure(monkeypatch):
    """The sentinel is a correct refusal, not a blocked attack."""
    monkeypatch.setattr(pipeline.llm_query, "generate_sql", lambda q, d=None: llm_query.LlmSql(
        sql="SELECT 'cannot answer' AS note", raw_response="...",
        duration_ms=5.0, model="m",
    ))
    answer = pipeline.answer_question("what is the weather in Tokyo?")
    assert answer.ok is False
    assert answer.route == "rejected"
    assert "can't answer that from this data" in answer.answer
    # It must NOT claim the query failed validation — that blames the wrong thing.
    assert "failed validation" not in answer.answer
