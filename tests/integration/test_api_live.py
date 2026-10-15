"""The API against the real loaded dataset."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from payops import health
from payops.api.main import app
from payops.config import get_settings
from payops.db.connection import get_engine

pytestmark = pytest.mark.integration
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "generated"


@pytest.fixture(scope="module")
def client():
    s = get_settings()
    if health.check_database(s).status != "ok":
        pytest.skip("PostgreSQL is not reachable")
    with get_engine(s).connect() as c:
        if c.execute(text("SELECT to_regclass('payments.dataset_runs')")).scalar() is None:
            pytest.skip("No dataset loaded")
    if not (DATA_DIR / "ground_truth.json").exists():
        pytest.skip("No generated dataset in data/generated")
    return TestClient(app)


@pytest.fixture(scope="module")
def truth():
    return json.loads((DATA_DIR / "ground_truth.json").read_text(encoding="utf-8"))


def test_meta_reports_the_loaded_dataset(client, truth):
    assert client.get("/api/v1/meta").json()["dataset_id"] == truth["dataset_id"]


def test_kpis_for_last_week(client):
    f = {x["name"]: x["value"] for x in client.get("/api/v1/kpis").json()["figures"]}
    assert f["attempts"] > 0
    assert f["failure_rate"] == pytest.approx(100 * f["failures"] / f["attempts"], abs=0.001)
    assert f["failure_rate"] > f["previous_failure_rate"]


def test_breakdown_ranks_the_injected_provider_first(client, truth):
    primary = next(i for i in truth["injected_incidents"] if i["key"] == "primary")
    rows = client.get("/api/v1/breakdown").json()["rows"]
    assert rows[0]["provider_code"] == primary["provider_code"]


def test_anomalies_endpoint_finds_the_incident_and_its_events(client, truth):
    primary = next(i for i in truth["injected_incidents"] if i["key"] == "primary")
    rows = client.get("/api/v1/anomalies").json()["rows"]
    assert [r["segment"] for r in rows] == [primary["provider_code"]]
    ids = {e["event_id"] for e in rows[0]["related_events"]}
    assert truth["deployments"]["temporally_correlated"] in ids


def test_hourly_timeseries_matches_kpis(client):
    rows = client.get("/api/v1/timeseries", params={"unit": "hour"}).json()["rows"]
    f = {x["name"]: x["value"] for x in client.get("/api/v1/kpis").json()["figures"]}
    assert sum(r["attempts"] for r in rows) == f["attempts"]


def test_investigation_end_to_end(client, truth):
    primary = next(i for i in truth["injected_incidents"] if i["key"] == "primary")
    r = client.post("/api/v1/investigations",
                    json={"question": "Why did the failure rate increase last week?", "use_llm": False})
    assert r.status_code == 201
    body = r.json()
    assert body["mode"] == "template" and body["status"] == "complete"
    assert body["report"]["hypotheses"][0]["title"].endswith(primary["provider_code"])
    assert primary["provider_code"] in body["text"]
