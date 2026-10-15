"""Investigation history in the real database: login permissions, save, resume.

Skipped unless APP_DB_PASSWORD is set and `python -m payops.db.setup_history` has run.
Rows these tests create are removed afterwards with the owner login (the history
login itself has no DELETE permission, on purpose).
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from payops import health
from payops.agents import graph
from payops.agents.graph import resume_investigation, run_investigation
from payops.config import get_settings
from payops.db.connection import get_engine, get_history_engine, get_readonly_engine
from payops.history.store import NotResumable, PostgresStore

pytestmark = pytest.mark.integration
Q = "[integration test] Why did the failure rate increase last week?"


@pytest.fixture(scope="module")
def settings():
    s = get_settings()
    if health.check_database(s).status != "ok":
        pytest.skip("PostgreSQL is not reachable")
    if not s.history_enabled:
        pytest.skip("APP_DB_PASSWORD not set (investigation history disabled)")
    if health.check_history(s).status != "ok":
        pytest.skip("History not set up: python -m payops.db.setup_history")
    with get_engine(s).connect() as c:
        if c.execute(text("SELECT to_regclass('payments.dataset_runs')")).scalar() is None:
            pytest.skip("No dataset loaded")
    yield s
    with get_engine(s).begin() as c:   # clean up this module's rows with the owner login
        c.execute(text("DELETE FROM ops.investigations WHERE question = :q"), {"q": Q})


@pytest.fixture(scope="module")
def store(settings):
    return PostgresStore(get_history_engine(settings))


def _fails(engine, sql):
    with pytest.raises(DBAPIError) as info:
        with engine.begin() as c:
            c.execute(text(sql))
    return str(info.value.orig).lower()


def test_history_login_cannot_read_payment_data(settings):
    assert "permission denied" in _fails(get_history_engine(settings), "SELECT count(*) FROM payments.transactions")


def test_history_login_cannot_delete_history(settings):
    assert "permission denied" in _fails(get_history_engine(settings), "DELETE FROM ops.investigations")


def test_readonly_login_cannot_see_history(settings):
    assert "permission denied" in _fails(get_readonly_engine(settings), "SELECT count(*) FROM ops.investigations")


def test_an_investigation_is_saved_with_every_step(settings, store):
    r = run_investigation(get_readonly_engine(settings), Q, store=store)
    assert r["persisted"] is True
    rec = store.get(r["investigation_id"])
    assert rec.status == "complete" and rec.result["report_text"] == r["report_text"]
    assert len(store.steps(r["investigation_id"])) == len(graph.NODES)


def test_a_partial_investigation_resumes_to_the_same_answer(settings, store, monkeypatch):
    engine = get_readonly_engine(settings)
    fresh = run_investigation(engine, Q, store=store)
    agent, fn, needs = graph.NODES["pattern"]

    def boom(*args):
        raise RuntimeError("simulated crash")

    monkeypatch.setitem(graph.NODES, "pattern", (agent, boom, needs))
    first = run_investigation(engine, Q, store=store)
    assert first["status"] == "partial"
    monkeypatch.setitem(graph.NODES, "pattern", (agent, fn, needs))

    resumed = resume_investigation(engine, store, first["investigation_id"])
    assert resumed["status"] == "complete" and resumed["reused_steps"] == ["forecast", "plan", "transaction"]
    for key in ("headline", "findings", "hypotheses", "anomalies", "report_text"):
        assert resumed[key] == fresh[key], key
    assert store.get(first["investigation_id"]).runs == 2
    with pytest.raises(NotResumable):
        resume_investigation(engine, store, first["investigation_id"])
