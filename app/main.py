"""FastAPI backend. Serves /ask, /health, /examples and the static chat UI.

Endpoints are deliberately sync `def`, not `async def`: the work underneath is
blocking (psycopg, and a CPU inference call that can take 30s+). FastAPI runs
sync handlers on a threadpool, so one slow question does not block the event
loop and freeze /health for everybody else.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import pipeline
import templates
from config import settings
from readiness import readiness, start_background_wait

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("mfg-chatbot")

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Kick off the readiness wait without blocking the server from binding.

    The UI must come up immediately so it can show 'still warming up' rather
    than a connection-refused page while the model loads.
    """
    log.info("ollama : %s (model %s)", settings.ollama_url, settings.ollama_model)
    log.info("dialect: %s", settings.dialect_name)
    log.info("guard  : max %d rows, %d ms statement timeout",
             settings.max_rows, settings.statement_timeout_ms)
    start_background_wait()
    yield


app = FastAPI(
    title="Manufacturing Data Chatbot",
    description="Natural-language questions over production data, answered by a "
                "local LLM. No cloud AI services are contacted.",
    version="1.0.0",
    lifespan=lifespan,
)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500,
                          description="A natural-language question about production data.")


@app.post("/ask")
def ask(request: AskRequest) -> JSONResponse:
    """Answer one question. Always returns a body — never a bare stack trace."""
    if not readiness.ready:
        # 503 with a readable message, so the UI can say something useful.
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "question": request.question,
                "route": "not_ready",
                "answer": readiness.blocking_message(),
                "detail": readiness.waiting_for(),
            },
        )

    answer = pipeline.answer_question(request.question)
    log.info("q=%r route=%s ok=%s rows=%d total=%.0fms",
             request.question[:80], answer.route, answer.ok,
             answer.row_count, answer.total_ms)
    return JSONResponse(status_code=200, content=answer.to_dict())


@app.get("/health")
def health() -> JSONResponse:
    """Readiness detail. 200 when answering, 503 while warming up or failed."""
    snapshot = readiness.snapshot()
    snapshot["model"] = settings.ollama_model
    snapshot["ollama_url"] = settings.ollama_url
    snapshot["dialect"] = settings.dialect_name
    return JSONResponse(status_code=200 if snapshot["ready"] else 503, content=snapshot)


@app.get("/examples")
def examples() -> dict:
    """Feeds the suggestion chips in the UI."""
    return {
        "templates": templates.template_ids(),
        "questions": [
            "How many good vs bad parts did Line A make last week?",
            "Reject breakdown by category for Line B last month",
            "Top 5 alarms on Line C in the last 30 days",
            "Which alarms caused the most downtime last month?",
            "How many batches did Line A run last week?",
            "Total parts produced yesterday",
            "What was the yield per line last month?",
            "Are there any ongoing alarms?",
            "Daily production for Line A over the last 14 days",
            "Which batches missed target by the most last month?",
            "Which product had the worst reject rate last month?",
            "What is the average batch duration in hours for each line?",
        ],
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(settings.static_dir / "index.html")


app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
