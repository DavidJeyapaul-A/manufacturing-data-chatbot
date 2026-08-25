"""One question in, one answer out. The orchestration layer.

    question
       |
       v
    match_template()  --hit-->  parameterised SQL
       | miss
       v
    llm_query.generate_sql()    model-written SQL
       |
       +------------> sql_guard.validate()  <-- BOTH paths, always
                             |
                             v
                      run_select()  (readonly role, statement timeout)
                             |
                             v
                      responder.summarise()

The guard sits on the join, not on one branch, so there is no route to the
database that skips it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import llm_query
import responder
import sql_guard
import templates
from config import settings
from db.connection import QueryExecutionError, run_select
from db.dialect import get_dialect
from readiness import readiness

log = logging.getLogger(__name__)


@dataclass
class Answer:
    """The full response envelope. Everything the UI's detail panel shows."""

    ok: bool
    question: str
    answer: str
    route: str                      # 'template' | 'llm' | 'rejected' | 'error'
    sql: str = ""
    #: The values bound to the '?' markers in `sql`. Shown in the UI next to the
    #: SQL: it is the visible proof that user text is parameterised, not pasted.
    params: list[Any] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    execution_ms: float = 0.0
    total_ms: float = 0.0
    llm_ms: float | None = None
    template_id: str | None = None
    model: str | None = None
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _jsonable(value: Any) -> Any:
    """Make driver types safe for JSON without losing precision in the UI."""
    if isinstance(value, Decimal):
        # str, not float: keeps 99.35 from becoming 99.34999999999999.
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ", timespec="seconds") if isinstance(value, datetime) \
            else value.isoformat()
    return value


REJECTION_MESSAGE = "I couldn't safely answer that."


def answer_question(question: str, now: datetime | None = None) -> Answer:
    """Run the full pipeline for one question. Never raises."""
    started = time.perf_counter()
    question = (question or "").strip()
    dialect = get_dialect()

    if not question:
        return Answer(
            ok=False, question=question, route="rejected",
            answer="Ask me something about production, batches, parts or alarms.",
        )

    # ---- route 1: deterministic templates --------------------------------
    match = templates.match_template(question, now=now, dialect=dialect)
    llm_ms: float | None = None
    model_name: str | None = None
    template_id: str | None = None
    context: dict[str, Any] = {}

    if match is not None:
        candidate_sql = match.sql
        params: list[Any] = match.params
        template_id = match.template_id
        context = match.context
        route = "template"
        log.info("template hit: %s", template_id)
    else:
        # ---- route 2: local LLM ------------------------------------------
        route = "llm"
        log.info("no template matched, falling back to the model")
        readiness.wait_for_warm_model(timeout=float(settings.ollama_timeout))
        try:
            produced = llm_query.generate_sql(question, dialect)
        except llm_query.LlmUnavailable as exc:
            return _finish(Answer(
                ok=False, question=question, route="error",
                answer="The local model isn't answering, so I can't handle that question yet.",
                detail=str(exc),
            ), started)
        candidate_sql = produced.sql
        params = []
        llm_ms = produced.duration_ms
        model_name = produced.model
        if not candidate_sql:
            return _finish(Answer(
                ok=False, question=question, route="rejected", llm_ms=llm_ms,
                model=model_name, answer=REJECTION_MESSAGE,
                detail="The model returned an empty response instead of a SELECT statement.",
            ), started)

    # The prompt tells the model to emit this sentinel when the schema cannot
    # answer the question. That is a correct refusal, not a safety violation —
    # reporting it as "failed validation" blames the wrong thing.
    if route == "llm" and "cannot answer" in candidate_sql.lower():
        return _finish(Answer(
            ok=False, question=question, route="rejected", llm_ms=llm_ms,
            model=model_name, sql=candidate_sql,
            answer="I can't answer that from this data. I only have production "
                   "batches, part counts and alarms for the three lines.",
            detail="The model reported that the question cannot be answered from this schema.",
        ), started)

    # ---- the guard: both routes, no exceptions ---------------------------
    try:
        guarded = sql_guard.validate(candidate_sql, dialect=dialect)
    except sql_guard.SqlRejected as exc:
        # No auto-repair, by design. Show what was rejected and why.
        return _finish(Answer(
            ok=False, question=question, route="rejected", sql=candidate_sql,
            llm_ms=llm_ms, model=model_name, template_id=template_id,
            answer=f"{REJECTION_MESSAGE} The generated query failed validation: {exc.reason}.",
            detail=exc.detail or exc.reason,
        ), started)

    # ---- execute on the readonly connection ------------------------------
    try:
        result = run_select(guarded.sql, params, dialect=dialect)
    except QueryExecutionError as exc:
        return _finish(Answer(
            ok=False, question=question, route="error", sql=guarded.sql,
            llm_ms=llm_ms, model=model_name, template_id=template_id,
            answer="The query was safe but the database could not run it.",
            detail=str(exc),
        ), started)

    # ---- phrase it -------------------------------------------------------
    prose = responder.summarise(result, template_id=template_id, context=context)

    return _finish(Answer(
        ok=True, question=question, route=route, answer=prose, sql=guarded.sql,
        params=[_jsonable(p) for p in params],
        columns=result.columns,
        rows=[[_jsonable(cell) for cell in row] for row in result.rows],
        row_count=result.row_count, execution_ms=result.execution_ms,
        llm_ms=llm_ms, template_id=template_id, model=model_name,
        truncated=result.truncated, notes=guarded.notes,
    ), started)


def _finish(answer: Answer, started: float) -> Answer:
    answer.total_ms = round((time.perf_counter() - started) * 1000, 1)
    return answer


__all__ = ["answer_question", "Answer", "REJECTION_MESSAGE"]
