"""History endpoints with the real workflow, canned queries and an in-memory store."""

import pytest
from fastapi.testclient import TestClient

import fakes
from auth_helpers import sign_in_as
from payops.agents import graph
from payops.api import routes_investigations
from payops.api.main import app
from payops.config import get_settings
from payops.history.store import MemoryStore

Q = {"question": "Why did the failure rate increase last week?", "use_llm": False}


@pytest.fixture
def store(monkeypatch):
    fakes.install(monkeypatch)
    monkeypatch.setattr(routes_investigations, "get_readonly_engine", lambda settings: None)   # canned queries only
    s = MemoryStore()
    app.dependency_overrides[routes_investigations.get_store] = lambda: s
    sign_in_as(app, "lead")
    yield s
    app.dependency_overrides.clear()


@pytest.fixture
def client(store):
    return TestClient(app)


def test_a_new_investigation_is_saved_listed_and_reopened(client, store):
    created = client.post("/api/v1/investigations", json=Q).json()
    assert created["persisted"] is True and created["run"] == 1
    items = client.get("/api/v1/investigations").json()["items"]
    assert [i["investigation_id"] for i in items] == [created["investigation_id"]]
    assert items[0]["status"] == "complete" and items[0]["resumable"] is False and "result" not in items[0]

    saved = client.get(f"/api/v1/investigations/{created['investigation_id']}").json()
    assert saved["result"]["report_text"] == created["text"] and saved["question"] == Q["question"]


def test_list_filters_by_status_and_pages(client, store):
    for _ in range(3):
        client.post("/api/v1/investigations", json=Q)
    assert len(client.get("/api/v1/investigations", params={"limit": 2}).json()["items"]) == 2
    assert client.get("/api/v1/investigations", params={"status": "partial"}).json()["items"] == []
    assert client.get("/api/v1/investigations", params={"status": "bogus"}).status_code == 422
    assert client.get("/api/v1/investigations", params={"limit": 1000}).status_code == 422


def test_resume_over_http(client, store, monkeypatch):
    agent, fn, needs = graph.NODES["pattern"]

    def boom(*a):
        raise RuntimeError("boom")

    monkeypatch.setitem(graph.NODES, "pattern", (agent, boom, needs))
    first = client.post("/api/v1/investigations", json=Q).json()
    assert first["status"] == "partial"
    monkeypatch.setitem(graph.NODES, "pattern", (agent, fn, needs))

    r = client.post(f"/api/v1/investigations/{first['investigation_id']}/resume")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "complete" and body["run"] == 2 and "plan" in body["reused_steps"]
    assert body["question"] == Q["question"]


def test_resuming_a_complete_investigation_is_a_conflict(client):
    created = client.post("/api/v1/investigations", json=Q).json()
    r = client.post(f"/api/v1/investigations/{created['investigation_id']}/resume")
    assert r.status_code == 409 and "complete" in r.json()["detail"]


def test_unknown_and_malformed_ids(client):
    assert client.get("/api/v1/investigations/00000000-0000-4000-8000-00000000dead").status_code == 404
    assert client.post("/api/v1/investigations/00000000-0000-4000-8000-00000000dead/resume").status_code == 404
    assert client.get("/api/v1/investigations/not-a-uuid").status_code == 422
    assert client.get("/api/v1/investigations/..%2F..%2Fetc").status_code in {404, 422}


def test_without_history_configured_listing_explains_how_to_enable_it(monkeypatch):
    fakes.install(monkeypatch)
    app.dependency_overrides[routes_investigations.get_store] = lambda: None
    sign_in_as(app, "lead")
    try:
        r = TestClient(app).get("/api/v1/investigations")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 503 and "APP_DB_PASSWORD" in r.json()["detail"]


def test_too_many_investigations_at_once_get_429(client):
    slots = get_settings().max_concurrent_investigations
    taken = [routes_investigations.SLOTS.acquire(slots) for _ in range(slots)]
    try:
        assert all(taken)
        r = client.post("/api/v1/investigations", json=Q)
        assert r.status_code == 429 and "try again" in r.json()["detail"]
    finally:
        for _ in taken:
            routes_investigations.SLOTS.release()
    assert client.post("/api/v1/investigations", json=Q).status_code == 201     # a slot is free again
