"""The LLM fallback path: local Ollama, schema prompt, few-shot, SELECT only.

Reached only when no template matched. Everything it produces still goes
through sql_guard before it touches the database — the model is treated as an
untrusted SQL generator, because that is exactly what it is.

Talks plain HTTP to Ollama's /api/generate. No SDK, no cloud, no telemetry: the
only host this module ever contacts is OLLAMA_URL, which is a compose service
name on a private network.
"""

from __future__ import annotations

import functools
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from config import settings
from db.dialect import Dialect, get_dialect
from semantic import load_semantic_model
from sql_guard import strip_markdown_fence

log = logging.getLogger(__name__)


class LlmUnavailable(RuntimeError):
    """Ollama could not be reached, or did not answer in time."""


@dataclass
class LlmSql:
    """What the model produced, plus everything needed to debug it in the UI."""

    sql: str
    raw_response: str
    duration_ms: float
    model: str
    prompt: str = field(repr=False, default="")


# --------------------------------------------------------------------------- #
# Few-shot examples — one file per dialect
# --------------------------------------------------------------------------- #

@functools.lru_cache(maxsize=4)
def load_examples(fewshot_file: str) -> tuple[tuple[str, str], ...]:
    """Load (question, sql) pairs for the active dialect."""
    path = Path(settings.fewshot_dir) / fewshot_file
    if not path.exists():
        raise FileNotFoundError(
            f"Few-shot examples not found at {path}. Each dialect needs its own "
            f"file — see db/dialect.py: Dialect.fewshot_file."
        )
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    pairs = [
        (str(item["question"]).strip(), str(item["sql"]).strip())
        for item in (raw.get("examples") or [])
        if item.get("question") and item.get("sql")
    ]
    if not pairs:
        raise ValueError(f"{path} contains no usable examples.")
    return tuple(pairs)


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

_SYSTEM = """You are a SQL generator for a manufacturing production database.
You translate one question into one {vendor} SELECT statement.

HARD RULES — a reply that breaks any of these is discarded:
1. Output ONLY the SQL. No prose, no explanation, no markdown fences, no
   comments, and no trailing semicolon.
2. Exactly ONE statement, and it must start with SELECT.
3. Use ONLY the tables and columns listed in the schema below. Never invent a
   column. Never query system catalogues.
4. Never write INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, GRANT or any other
   statement that changes anything. The connection is read-only and it will fail.
5. Always return at most {max_rows} rows.
6. The column part_counts."count" is a reserved word — always write it quoted
   as "count".
7. If the question cannot be answered from this schema, output exactly:
   SELECT 'cannot answer' AS note
"""

_PROMPT = """{system}
=== SCHEMA ===
{schema}

=== EXAMPLES ===
{examples}

=== QUESTION ===
{question}

=== {vendor} SQL ==="""


def build_prompt(question: str, dialect: Dialect | None = None) -> str:
    """Assemble the full prompt: rules + semantic layer + few-shot + question."""
    dialect = dialect or get_dialect()
    model = load_semantic_model()
    vendor = "PostgreSQL" if dialect.sqlglot_dialect == "postgres" else "T-SQL"

    examples = "\n\n".join(
        f"Q: {q}\nA: {sql}" for q, sql in load_examples(dialect.fewshot_file)
    )
    system = _SYSTEM.format(vendor=vendor, max_rows=settings.max_rows)

    return _PROMPT.format(
        system=system,
        schema=model.render_for_prompt(),
        examples=examples,
        question=question.strip(),
        vendor=vendor.upper(),
    )


# --------------------------------------------------------------------------- #
# Ollama HTTP
# --------------------------------------------------------------------------- #

def _generate(prompt: str, timeout: float, num_predict: int = 400) -> dict[str, Any]:
    """POST /api/generate. Raises LlmUnavailable on any transport failure."""
    payload = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "options": {
            # Deterministic: the same question must produce the same SQL every
            # time, or the demo is not reproducible.
            "temperature": 0,
            "top_p": 1,
            "seed": 42,
            "num_predict": num_predict,
            # The model stops as soon as it starts explaining itself.
            "stop": ["```", "Q:", "=== ", "\nExplanation"],
        },
    }
    url = f"{settings.ollama_url}/api/generate"
    try:
        response = httpx.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException as exc:
        raise LlmUnavailable(
            f"the model did not respond within {timeout:.0f}s. CPU inference on "
            f"{settings.ollama_model} is slow — raise OLLAMA_TIMEOUT or use a smaller model."
        ) from exc
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text.strip()[:200]
        raise LlmUnavailable(
            f"Ollama returned HTTP {exc.response.status_code} for model "
            f"'{settings.ollama_model}': {detail}"
        ) from exc
    except httpx.HTTPError as exc:
        raise LlmUnavailable(
            f"could not reach Ollama at {settings.ollama_url} ({exc}). Inside the app "
            f"container this must be the compose service name, not localhost."
        ) from exc


def generate_sql(question: str, dialect: Dialect | None = None) -> LlmSql:
    """Ask the local model for a SELECT. The result is NOT yet trusted."""
    dialect = dialect or get_dialect()
    prompt = build_prompt(question, dialect)

    started = time.perf_counter()
    body = _generate(prompt, timeout=float(settings.ollama_timeout))
    elapsed_ms = (time.perf_counter() - started) * 1000

    raw = (body.get("response") or "").strip()
    log.info("llm produced %d chars in %.0f ms", len(raw), elapsed_ms)

    return LlmSql(
        sql=_clean(raw),
        raw_response=raw,
        duration_ms=round(elapsed_ms, 1),
        model=settings.ollama_model,
        prompt=prompt,
    )


def _clean(text: str) -> str:
    """Strip the wrappers small models add despite being told not to.

    This is formatting cleanup only — unfencing and de-labelling. It never
    rewrites SQL: if what is left is not a valid single SELECT, sql_guard
    rejects it and the user is told so. We do not repair bad SQL.
    """
    candidate = strip_markdown_fence(text)
    for prefix in ("sql:", "a:", "answer:", "query:"):
        if candidate.lower().startswith(prefix):
            candidate = candidate[len(prefix):].strip()
    # Keep from the first SELECT/WITH onward; models like to preface.
    lowered = candidate.lower()
    starts = [i for i in (lowered.find("select"), lowered.find("with")) if i > 0]
    if not lowered.startswith(("select", "with")) and starts:
        candidate = candidate[min(starts):]
    return candidate.strip().rstrip(";").strip()


# --------------------------------------------------------------------------- #
# Readiness
# --------------------------------------------------------------------------- #

def warm_prompt_cache(dialect: Dialect | None = None) -> float:
    """Push the fixed prompt prefix through the model once, so it is cached.

    Our prompt is a large constant prefix (rules + schema + few-shot examples)
    followed by the question. Ollama caches the KV state of a prefix it has
    already seen, and on CPU that prefix is where nearly all the time goes:
    measured cold, a first question took 149 s; the next took 11 s.

    So the readiness probe proving the model *responds* is not enough — it uses
    a trivial prompt and leaves the real prefix cold, which hands the first
    person to ask a question the entire 149 s. Doing it here, in the background
    after readiness passes, means they get the 11 s instead.
    """
    prompt = build_prompt("warm up the prompt cache", dialect or get_dialect())
    started = time.perf_counter()
    try:
        # num_predict=1: we want the prefix processed, not an answer.
        _generate(prompt, timeout=float(settings.ollama_timeout), num_predict=1)
    except LlmUnavailable as exc:
        log.warning("prompt cache warm-up did not finish: %s", exc)
        return 0.0
    elapsed = time.perf_counter() - started
    log.info("prompt cache warmed in %.1fs — LLM questions now skip prefix processing",
             elapsed)
    return elapsed


def check_model_ready() -> tuple[bool, str]:
    """Prove the model ANSWERS, not merely that the port is open.

    A real one-token generation also warms the weights into RAM, so the first
    demo question is not the one that pays the cold-load cost.
    """
    try:
        tags = httpx.get(f"{settings.ollama_url}/api/tags", timeout=10).json()
    except httpx.HTTPError as exc:
        return False, f"cannot reach Ollama at {settings.ollama_url}: {exc}"

    available = [m.get("name", "") for m in (tags.get("models") or [])]
    if settings.ollama_model not in available:
        return False, (
            f"model '{settings.ollama_model}' is not in the ollama volume yet "
            f"(present: {', '.join(available) or 'none'}). The ollama-init service pulls it."
        )

    try:
        # Short budget: this is a liveness probe, not a query.
        body = _generate("Reply with the single word: ready", timeout=90.0, num_predict=5)
    except LlmUnavailable as exc:
        return False, str(exc)

    if not (body.get("response") or "").strip():
        return False, f"model '{settings.ollama_model}' returned an empty response"
    return True, f"model '{settings.ollama_model}' is loaded and responding"


__all__ = ["generate_sql", "build_prompt", "load_examples", "check_model_ready",
           "warm_prompt_cache", "LlmSql", "LlmUnavailable"]
