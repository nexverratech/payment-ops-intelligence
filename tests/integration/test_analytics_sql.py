"""Analytics against the loaded dataset: SQL results equal pandas on the CSV,
and the deterministic pipeline finds the injected incident (ground truth)."""

import json
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text

from payops import health
from payops.analytics import queries
from payops.analytics.detection import detect_anomalies
from payops.analytics.metrics import last_complete_week
from payops.analytics.models import AnalysisFilter
from payops.analytics.report import build_report, main, render_text
from payops.config import get_settings
from payops.db.connection import get_readonly_engine

pytestmark = pytest.mark.integration
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "generated"


@pytest.fixture(scope="module")
def conn():
    s = get_settings()
    if health.check_database(s).status != "ok":
        pytest.skip("PostgreSQL is not reachable")
    if not (DATA_DIR / "ground_truth.json").exists():
        pytest.skip("No generated dataset in data/generated")
    with get_readonly_engine(s).connect() as c:
        if c.execute(text("SELECT to_regclass('payments.dataset_runs')")).scalar() is None:
            pytest.skip("No dataset loaded")
        yield c


@pytest.fixture(scope="module")
def csv():
    df = pd.read_csv(DATA_DIR / "transactions.csv", usecols=["created_at", "provider_code", "payment_method", "status", "failure_reason", "amount"])
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
    return df


@pytest.fixture(scope="module")
def truth():
    return json.loads((DATA_DIR / "ground_truth.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def last_week(conn):
    start, end = last_complete_week(queries.dataset_window(conn)["window_end"])
    return AnalysisFilter(start=start, end=end)


def _in(df, f):
    return df[(df["created_at"] >= f.start) & (df["created_at"] < f.end)]


def test_summary_matches_pandas(conn, csv, last_week):
    s = queries.summary(conn, last_week)
    p = _in(csv, last_week)
    assert s["attempts"] == len(p)
    assert s["failures"] == (p["status"] == "FAILED").sum()
    assert s["failed_value"] == pytest.approx(p.loc[p["status"] == "FAILED", "amount"].sum(), abs=0.01)


@pytest.mark.parametrize("by, column", [("provider", "provider_code"), ("method", "payment_method")])
def test_breakdown_matches_pandas(conn, csv, last_week, by, column):
    sql = queries.breakdown(conn, last_week, by).set_index(column)
    p = _in(csv, last_week)
    expected = p.groupby(column).agg(attempts=("status", "size"), failures=("status", lambda s: (s == "FAILED").sum()))
    assert sql["attempts"].to_dict() == expected["attempts"].to_dict()
    assert sql["failures"].to_dict() == expected["failures"].to_dict()


def test_filters_by_provider_and_reason(conn, csv, last_week):
    f = last_week.model_copy(update={"provider_codes": ("KESTREL",), "failure_reasons": ("TIMEOUT",)})
    s = queries.summary(conn, f)
    p = _in(csv, f)
    p = p[p["provider_code"] == "KESTREL"]
    assert s["attempts"] == len(p)
    assert s["failures"] == ((p["status"] == "FAILED") & (p["failure_reason"] == "TIMEOUT")).sum()


def test_hourly_series_adds_up_to_the_summary(conn, last_week):
    hourly = queries.time_series(conn, last_week, "hour", by_provider=True)
    s = queries.summary(conn, last_week)
    assert hourly["attempts"].sum() == s["attempts"]
    assert hourly["failures"].sum() == s["failures"]


def test_detection_finds_every_injected_incident_and_nothing_else(conn, truth):
    window = queries.dataset_window(conn)
    hourly = queries.time_series(conn, AnalysisFilter(start=window["window_start"], end=window["window_end"]), "hour", by_provider=True)
    found = detect_anomalies(hourly)

    def overlaps(w, inc):
        return (w.segment == inc["provider_code"] and w.start < pd.Timestamp(inc["actual_end"])
                and w.end > pd.Timestamp(inc["actual_start"]))

    primary = next(i for i in truth["injected_incidents"] if i["key"] == "primary")
    assert any(overlaps(w, primary) for w in found), "primary incident not detected"
    false_alarms = [w for w in found if not any(overlaps(w, i) for i in truth["injected_incidents"])]
    assert false_alarms == []


def test_report_reaches_the_ground_truth_conclusion(conn, truth):
    r = build_report(conn)
    primary = next(i for i in truth["injected_incidents"] if i["key"] == "primary")
    assert r["top_contributor"]["provider_code"] == primary["provider_code"]
    assert r["top_contributor"]["reasons"][0]["failure_reason"] == primary["failure_reason"]
    [anomaly] = r["anomalies"]
    ids = {e["event_id"] for e in anomaly["related_events"]}
    assert truth["deployments"]["temporally_correlated"] in ids
    assert primary["provider_incident_id"] in ids
    assert all("not evidence of cause" in e["relationship"] for e in anomaly["related_events"])
    assert r["generated_by"] == "deterministic analytics (no LLM)"
    json.dumps(r)  # the whole report must be JSON-serialisable for the API
    assert "KESTREL" in render_text(r)


def test_report_command_runs(conn, capsys):  # `conn` makes this skip when no data is loaded
    assert main([]) == 0
    assert "PAYMENT FAILURE REVIEW" in capsys.readouterr().out
