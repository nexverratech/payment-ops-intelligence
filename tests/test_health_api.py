"""Tests for GET /health. Database and LLM checks are replaced with stubs,
so these tests need neither Docker nor Ollama."""

import os

import pytest
from fastapi.testclient import TestClient

from payops import health
from payops.api.main import app
from payops.health import ComponentHealth

OK = ComponentHealth(status="ok", latency_ms=1.0)
DOWN = ComponentHealth(status="unreachable", detail="OperationalError. Is the container running?")
MISSING = ComponentHealth(status="model_missing", detail="Model 'qwen3:4b' is not pulled.")


@pytest.fixture
def client():
    return TestClient(app)


NOT_CONFIGURED = ComponentHealth(status="not_configured", detail="APP_DB_PASSWORD not set")


def stub(monkeypatch, db, llm, history=NOT_CONFIGURED):
    monkeypatch.setattr(health, "check_database", lambda settings: db)
    monkeypatch.setattr(health, "check_llm", lambda settings: llm)
    monkeypatch.setattr(health, "check_history", lambda settings: history)


def test_all_ok(client, monkeypatch):
    stub(monkeypatch, OK, OK)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["llm_mode"] == "ollama"
    assert body["environment"] == "test"


def test_llm_down_means_degraded_fallback_not_failure(client, monkeypatch):
    stub(monkeypatch, OK, DOWN)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"
    assert r.json()["llm_mode"] == "fallback"


def test_model_not_pulled_is_reported(client, monkeypatch):
    stub(monkeypatch, OK, MISSING)
    body = client.get("/health").json()
    assert body["components"]["llm"]["status"] == "model_missing"
    assert body["llm_mode"] == "fallback"


def test_database_down_returns_503(client, monkeypatch):
    stub(monkeypatch, DOWN, OK)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "down"


def test_response_never_contains_password(client):
    # Real checks (not stubs): even the error path must not echo the password.
    password = os.environ["POSTGRES_PASSWORD"]
    assert password not in client.get("/health").text


def test_api_docs_page_is_disabled(client):
    # /docs would load JavaScript from a public CDN, breaking offline use.
    assert client.get("/docs").status_code == 404


def test_real_llm_check_reports_unreachable_without_raising():
    # Uses the real check against a closed localhost port: must return a status, never raise.
    from payops.config import Settings

    s = Settings(_env_file=None, postgres_password="pw", readonly_db_password="ro",
                 ollama_base_url="http://127.0.0.1:9",
                 health_check_timeout_seconds=1)
    result = health.check_llm(s)
    assert result.status == "unreachable"


def test_history_not_configured_is_still_ok(client, monkeypatch):
    stub(monkeypatch, OK, OK, NOT_CONFIGURED)
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["components"]["history"]["status"] == "not_configured"


def test_history_configured_but_unreachable_is_degraded(client, monkeypatch):
    stub(monkeypatch, OK, OK, DOWN)
    assert client.get("/health").json()["status"] == "degraded"


def test_history_check_without_password_does_not_connect(monkeypatch):
    from payops.config import Settings

    monkeypatch.delenv("APP_DB_PASSWORD", raising=False)   # conftest copies your real one from .env
    s = Settings(_env_file=None, postgres_password="pw", readonly_db_password="ro")
    assert health.check_history(s).status == "not_configured"
