"""API surface, exercised without a database or a model.

The readiness gate and the error envelopes are the parts a demo actually hits
when something is not up yet, so they are worth testing directly.
"""

import pytest
from fastapi.testclient import TestClient

import main
import pipeline
from readiness import readiness


@pytest.fixture
def client(monkeypatch):
    """A client whose readiness state the test controls.

    The real startup hook launches a background prober that would race these
    tests (and fail anyway, with no database to reach), so it is stubbed out.
    """
    monkeypatch.setattr(main, "start_background_wait", lambda *a, **k: None)
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def warming():
    """Force the 'not ready yet' state."""
    readiness.database.ready = False
    readiness.model.ready = False
    readiness.database.message = "connection refused"
    readiness.model.message = "model is not loaded"
    readiness.state = "starting"
    yield


@pytest.fixture
def ready(monkeypatch):
    readiness.database.ready = True
    readiness.model.ready = True
    readiness.state = "ready"
    yield


def test_health_reports_503_and_the_outstanding_check(client, warming):
    response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["checks"]["database"]["ready"] is False
    assert "connection refused" in body["checks"]["database"]["message"]


def test_health_is_200_once_ready(client, ready):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ready"] is True


def test_ask_is_refused_with_a_plain_message_while_warming(client, warming):
    response = client.post("/ask", json={"question": "how many parts yesterday?"})
    assert response.status_code == 503
    body = response.json()
    assert body["route"] == "not_ready"
    assert "warming up" in body["answer"].lower()
    # The message must name what is missing, not just say 'unavailable'.
    assert "database" in body["answer"] or "model" in body["answer"]


def test_ask_returns_the_answer_envelope(client, ready, monkeypatch):
    """The UI needs sql / row_count / execution_ms on every successful answer."""
    fake = pipeline.Answer(
        ok=True, question="q", answer="42 parts.", route="template",
        sql="SELECT 1 FROM batches LIMIT 200", columns=["parts"], rows=[[42]],
        row_count=1, execution_ms=3.2, total_ms=4.0, template_id="production_totals",
    )
    monkeypatch.setattr(pipeline, "answer_question", lambda q: fake)
    monkeypatch.setattr(main.pipeline, "answer_question", lambda q: fake)

    response = client.post("/ask", json={"question": "total parts yesterday"})
    assert response.status_code == 200
    body = response.json()
    for field in ("sql", "row_count", "execution_ms", "route", "answer"):
        assert field in body
    assert body["answer"] == "42 parts."


def test_ask_rejects_an_empty_question(client, ready):
    assert client.post("/ask", json={"question": ""}).status_code == 422


def test_examples_endpoint_lists_templates_and_questions(client):
    body = client.get("/examples").json()
    assert len(body["questions"]) >= 10
    assert "good_vs_bad" in body["templates"]


def test_index_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Manufacturing Data Chatbot" in response.text
    # The SQL panel is the core of the demo — it must be in the shipped page.
    assert "Show SQL, timing and rows" in response.text
