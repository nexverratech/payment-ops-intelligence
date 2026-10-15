"""API endpoints with the database replaced by canned data (tests/fakes.py).

Checks validation, response shapes, period logic and error handling.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError, OperationalError

import fakes
from payops.analytics import queries
from payops.api import deps, routes_investigations
from payops.api.main import app


def _no_db():
    yield None  # stands in for a database connection


@pytest.fixture
def client(monkeypatch):
    fakes.install(monkeypatch)
    app.dependency_overrides[deps.get_readonly_conn] = _no_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def figures(body):
    return {f["name"]: f for f in body["figures"]}


# ---- meta and KPIs --------------------------------------------------------------

def test_meta_lists_filter_values(client):
    body = client.get("/api/v1/meta").json()
    assert body["dataset_id"] == "test-dataset"
    assert [p["provider_code"] for p in body["providers"]] == ["KESTREL", "NOVAPAY"]
    assert "TIMEOUT" in body["failure_reasons"] and "CARD" in body["payment_methods"]


def test_kpis_default_to_last_complete_week_and_compare_with_the_week_before(client):
    body = client.get("/api/v1/kpis").json()
    assert body["current"]["start"].startswith("2026-09-21") and body["current"]["end"].startswith("2026-09-28")
    assert body["previous"]["start"].startswith("2026-09-14")
    f = figures(body)
    assert f["failure_rate"]["value"] == 5.0            # 50 / 1000
    assert f["previous_failure_rate"]["value"] == 2.0   # 20 / 1000
    assert f["succeeded"]["value"] == 950
    assert f["affected_transaction_value"]["kind"] == "measured"
    assert "not revenue" in f["affected_transaction_value"]["method"]


def test_previous_period_has_the_same_length(client):
    body = client.get("/api/v1/kpis", params={"start": "2026-09-21T00:00:00Z", "end": "2026-09-24T00:00:00Z"}).json()
    assert body["previous"]["start"].startswith("2026-09-18") and body["previous"]["end"].startswith("2026-09-21")


@pytest.mark.parametrize(
    "params, message",
    [
        ({"provider": "kestrel"}, "valid code"),
        ({"provider": "KESTREL'; DROP TABLE x;--"}, "valid code"),
        ({"start": "2026-09-21T00:00:00", "end": "2026-09-28T00:00:00Z"}, "timezone"),
        ({"start": "2026-09-28T00:00:00Z", "end": "2026-09-21T00:00:00Z"}, "after start"),
    ],
)
def test_bad_filters_are_rejected_with_a_readable_message(client, params, message):
    r = client.get("/api/v1/kpis", params=params)
    assert r.status_code == 422
    assert message in r.text


def test_only_allowed_groupings_and_time_units(client):
    assert client.get("/api/v1/breakdown", params={"by": "customer_id"}).status_code == 422
    assert client.get("/api/v1/timeseries", params={"unit": "minute"}).status_code == 422


# ---- tables -------------------------------------------------------------------------

def test_breakdown_ranks_the_provider_that_got_worse_first(client):
    rows = client.get("/api/v1/breakdown").json()["rows"]
    top = rows[0]
    assert top["provider_code"] == "KESTREL"
    assert top["excess_failures"] == pytest.approx(30)      # 40 - 500 x 0.02
    assert top["contribution_share"] == pytest.approx(1.0)
    assert top["failed_value"] == 2000.0


def test_failure_reasons_include_change(client):
    rows = {r["failure_reason"]: r for r in client.get("/api/v1/failure-reasons").json()["rows"]}
    assert rows["TIMEOUT"]["change"] == 30
    assert rows["INSUFFICIENT_FUNDS"]["change"] == 0


def test_timeseries_adds_failure_rate_and_bucket(client):
    rows = client.get("/api/v1/timeseries", params={"unit": "hour"}).json()["rows"]
    assert rows and {"bucket", "failure_rate", "provider_code"} <= set(rows[0])
    assert rows[0]["failure_rate"] == pytest.approx(0.03)


def test_three_hour_buckets_are_accepted(client):
    r = client.get("/api/v1/timeseries", params={"unit": "3h"})
    assert r.status_code == 200 and r.json()["rows"]


def test_events_combine_deployments_and_notices(client):
    kinds = [r["kind"] for r in client.get("/api/v1/events").json()["rows"]]
    assert kinds == ["deployment", "provider_notice"]


def test_anomalies_find_the_spike_and_relate_events(client):
    rows = client.get("/api/v1/anomalies").json()["rows"]
    assert len(rows) == 1
    assert rows[0]["segment"] == "KESTREL"
    assert {e["event_id"] for e in rows[0]["related_events"]} == {"DEP-1", "INC-3"}


# ---- investigations -----------------------------------------------------------------

@pytest.fixture
def fake_report(monkeypatch):
    monkeypatch.setattr(routes_investigations, "build_report", lambda conn, as_of: {"top_contributor": {"provider_code": "KESTREL"}})
    monkeypatch.setattr(routes_investigations, "render_text", lambda report: "REPORT TEXT")


def test_investigation_returns_a_labelled_deterministic_result(client, fake_report):
    r = client.post("/api/v1/investigations", json={"question": "  Why did   failures rise\nlast week?  "})
    assert r.status_code == 201
    body = r.json()
    assert body["mode"] == "deterministic"
    assert body["question"] == "Why did failures rise last week?"   # whitespace collapsed
    assert "no LLM" in body["note"]
    assert body["text"] == "REPORT TEXT" and len(body["investigation_id"]) == 36


@pytest.mark.parametrize(
    "payload",
    [{"question": "why"}, {"question": "x" * 2001}, {"question": "Why did failures rise?", "as_of": "2026-09-29T00:00:00"}, {}],
)
def test_investigation_input_is_validated(client, fake_report, payload):
    assert client.post("/api/v1/investigations", json=payload).status_code == 422


# ---- errors -------------------------------------------------------------------------

def test_database_down_gives_503_without_internals(client, monkeypatch):
    def broken(conn, f):
        raise OperationalError("SELECT ...", {}, Exception("password=hunter2 connection refused"))

    monkeypatch.setattr(queries, "summary", broken)
    r = client.get("/api/v1/kpis")
    assert r.status_code == 503
    assert "hunter2" not in r.text and "SELECT" not in r.text


def test_query_error_gives_500_without_internals(client, monkeypatch):
    def broken(conn, f, by):
        raise DBAPIError("SELECT secret_column", {}, Exception("relation does not exist"))

    monkeypatch.setattr(queries, "breakdown", broken)
    r = client.get("/api/v1/breakdown")
    assert r.status_code == 500
    assert r.json() == {"detail": "Database query failed."}
