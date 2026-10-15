"""The agent workflow on the real loaded dataset, scored against ground truth."""

import json
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text

from payops import health
from payops.agents.graph import run_investigation
from payops.config import get_settings
from payops.db.connection import get_engine, get_readonly_engine

pytestmark = pytest.mark.integration
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "generated"


@pytest.fixture(scope="module")
def result():
    s = get_settings()
    if health.check_database(s).status != "ok":
        pytest.skip("PostgreSQL is not reachable")
    with get_engine(s).connect() as c:
        if c.execute(text("SELECT to_regclass('payments.dataset_runs')")).scalar() is None:
            pytest.skip("No dataset loaded")
    if not (DATA_DIR / "ground_truth.json").exists():
        pytest.skip("No generated dataset in data/generated")
    return run_investigation(get_readonly_engine(s), "Why did the payment failure rate increase last week?")


@pytest.fixture(scope="module")
def truth():
    return json.loads((DATA_DIR / "ground_truth.json").read_text(encoding="utf-8"))


def test_workflow_completes(result):
    assert result["status"] == "complete", result["errors"]


def test_leading_hypothesis_matches_ground_truth(result, truth):
    primary = next(i for i in truth["injected_incidents"] if i["key"] == "primary")
    leading = result["hypotheses"][0]
    assert leading["hypothesis_id"] == "H-PROVIDER"
    assert primary["provider_code"] in leading["title"]


def test_correlated_deployment_is_not_blamed(result, truth):
    dep = truth["deployments"]["temporally_correlated"]
    h = next(h for h in result["hypotheses"] if h["hypothesis_id"] == f"H-{dep}")
    assert h["status"] in {"alternative", "unlikely"}
    assert h["rank"] > 1


def test_anomaly_window_overlaps_the_injected_incident(result, truth):
    primary = next(i for i in truth["injected_incidents"] if i["key"] == "primary")
    a = result["anomalies"][0]
    ts = pd.Timestamp
    assert a["segment"] == primary["provider_code"]
    assert ts(a["start"]) < ts(primary["actual_end"]) and ts(a["end"]) > ts(primary["actual_start"])


def test_all_evidence_is_traceable(result):
    ok = {c["call_id"] for c in result["tool_calls"] if c["status"] == "ok"}
    for f in result["findings"]:
        assert f["evidence"]["tool_call_ids"] and set(f["evidence"]["tool_call_ids"]) <= ok


def test_no_tool_call_was_denied(result):
    assert all(c["status"] == "ok" for c in result["tool_calls"])
