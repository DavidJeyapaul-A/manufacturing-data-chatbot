"""Startup readiness: the app refuses to answer until the stack really works.

compose already gates `app` behind the seed and the model pull. This is the
second, independent check — it runs inside the app process and verifies the two
things the app itself depends on:

  1. the DATABASE is reachable AS THE READONLY ROLE and the tables have rows
  2. the MODEL actually generates a token (not merely that port 11434 is open)

Until both pass, /ask returns 503 with a plain-English message and /health says
which check is outstanding. If the retry budget runs out, the state flips to
'failed' with the underlying error — a loud failure, never a silent hang.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

# Exponential backoff, capped. Budget is generous because a cold 7B model on CPU
# can genuinely take a minute or two to produce its first token.
INITIAL_DELAY = 2.0
MAX_DELAY = 20.0
MAX_ATTEMPTS = 60


@dataclass
class Check:
    name: str
    ready: bool = False
    message: str = "not checked yet"
    attempts: int = 0


@dataclass
class Readiness:
    """Thread-safe snapshot of what is and is not ready."""

    database: Check = field(default_factory=lambda: Check("database"))
    model: Check = field(default_factory=lambda: Check("model"))
    state: str = "starting"          # starting | ready | failed
    #: Set when the prompt-cache warm-up STARTS, and again when it FINISHES.
    #: The LLM path waits only while a warm-up is genuinely in flight — waiting
    #: on one that was never started (a bare process, a dead prober) would block
    #: every question for the full timeout instead of merely being slow.
    llm_warm_started: threading.Event = field(default_factory=threading.Event, repr=False)
    llm_warm: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def wait_for_warm_model(self, timeout: float) -> None:
        """Block only if a prompt-cache warm-up is currently running.

        Ollama serves one request at a time, so issuing alongside the warm-up
        means queueing behind it and probably exhausting our own HTTP timeout.
        Waiting costs the same wall clock and then runs against a warm cache.
        """
        if self.llm_warm_started.is_set() and not self.llm_warm.is_set():
            log.info("readiness: holding the LLM query until the prompt cache is warm")
            self.llm_warm.wait(timeout=timeout)

    @property
    def ready(self) -> bool:
        return self.database.ready and self.model.ready

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "ready": self.ready,
                "checks": {
                    c.name: {"ready": c.ready, "message": c.message, "attempts": c.attempts}
                    for c in (self.database, self.model)
                },
            }

    def waiting_for(self) -> str:
        with self._lock:
            pending = [c.name for c in (self.database, self.model) if not c.ready]
        return " and ".join(pending) if pending else "nothing"

    def blocking_message(self) -> str:
        """What to tell a user who asked a question too early."""
        with self._lock:
            if self.state == "failed":
                broken = [
                    f"{c.name}: {c.message}"
                    for c in (self.database, self.model)
                    if not c.ready
                ]
                return (
                    "The chatbot could not start. "
                    + " ".join(broken)
                    + " Check `docker compose logs` for the failing service."
                )
            pending = [
                f"{c.name} ({c.message})"
                for c in (self.database, self.model)
                if not c.ready
            ]
            return (
                "Still warming up — waiting for " + " and ".join(pending) + ". "
                "The first model load on CPU can take a minute or two. Try again shortly."
            )

    def _update(self, check: Check, ready: bool, message: str) -> None:
        with self._lock:
            check.ready = ready
            check.message = message
            check.attempts += 1


readiness = Readiness()


def _probe(check: Check, probe: Callable[[], tuple[bool, str]]) -> bool:
    try:
        ok, message = probe()
    except Exception as exc:  # a probe must never kill the waiter thread
        ok, message = False, f"{type(exc).__name__}: {exc}"
    readiness._update(check, ok, message)
    if ok:
        log.info("readiness: %s OK — %s", check.name, message)
    else:
        log.warning("readiness: %s not ready — %s", check.name, message)
    return ok


def wait_for_dependencies(state: Readiness = readiness) -> bool:
    """Block until both checks pass or the budget is exhausted.

    Runs on a background thread so uvicorn can serve /health (and the UI banner)
    while the model is still loading.
    """
    from db.connection import check_database_ready
    from llm_query import check_model_ready, warm_prompt_cache

    delay = INITIAL_DELAY
    log.info("readiness: waiting for the database and the model...")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        db_ok = state.database.ready or _probe(state.database, check_database_ready)
        model_ok = state.model.ready or _probe(state.model, check_model_ready)

        if db_ok and model_ok:
            with state._lock:
                state.state = "ready"
            log.info("readiness: everything is up — the chatbot is answering questions.")
            # Ready FIRST, then warm. Template questions are already answerable
            # and must not wait behind a minutes-long prefix pass on CPU.
            state.llm_warm_started.set()
            try:
                warm_prompt_cache()
            finally:
                # Set even on failure: the LLM path should proceed and report a
                # real error, not block forever on a warm-up that will not come.
                state.llm_warm.set()
            return True

        log.info(
            "readiness: attempt %d/%d, waiting %.0fs (outstanding: %s)",
            attempt, MAX_ATTEMPTS, delay, state.waiting_for(),
        )
        time.sleep(delay)
        delay = min(delay * 1.5, MAX_DELAY)

    with state._lock:
        state.state = "failed"
    state.llm_warm.set()
    log.error("readiness: FAILED after %d attempts. Outstanding: %s",
              MAX_ATTEMPTS, state.waiting_for())
    return False


def start_background_wait(state: Readiness = readiness) -> threading.Thread:
    thread = threading.Thread(
        target=wait_for_dependencies, args=(state,),
        name="readiness-wait", daemon=True,
    )
    thread.start()
    return thread


__all__ = ["readiness", "Readiness", "wait_for_dependencies", "start_background_wait"]
