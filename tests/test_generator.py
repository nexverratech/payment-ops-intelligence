"""Tests for the synthetic data generator. No database needed.

A smaller configuration (40 days, 1,500 payments/day) keeps these fast;
the same code produces the full 90-day dataset.
"""

import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import pandas as pd

from payops.data.generator import generate, write_dataset
from payops.data.scenario import GenerationConfig

SMALL = GenerationConfig(seed=7, days=40, base_daily_volume=1_500)


@lru_cache(maxsize=1)
def small_data():
    return generate(SMALL)


def _ts(iso: str) -> pd.Timestamp:
    return pd.Timestamp(iso)


def _primary():
    return next(i for i in small_data().ground_truth["injected_incidents"] if i["key"] == "primary")


# ---- Reproducibility --------------------------------------------------------

def test_same_seed_gives_byte_identical_files(tmp_path):
    m1 = write_dataset(generate(SMALL), SMALL, tmp_path / "a")
    m2 = write_dataset(generate(SMALL), SMALL, tmp_path / "b")
    assert m1["files"] == m2["files"]  # same SHA-256 for every file
    assert m1["dataset_id"] == m2["dataset_id"]


def test_different_seed_gives_different_data(tmp_path):
    other = GenerationConfig(seed=8, days=40, base_daily_volume=1_500)
    m1 = write_dataset(small_data(), SMALL, tmp_path / "a")
    m2 = write_dataset(generate(other), other, tmp_path / "b")
    assert m1["files"]["transactions.csv"]["sha256"] != m2["files"]["transactions.csv"]["sha256"]


def test_manifest_records_seed_and_parameters_but_not_the_answer_key(tmp_path):
    manifest = write_dataset(small_data(), SMALL, tmp_path)
    assert manifest["seed"] == 7
    assert manifest["parameters"]["days"] == 40
    # Incident definitions (the answer key) live only in ground_truth.json.
    assert "incidents" not in manifest["parameters"]
    text = (tmp_path / "manifest.json").read_text()
    assert "peak_extra_failure_rate" not in text and "actual_start" not in text
    truth = json.loads((tmp_path / "ground_truth.json").read_text())
    assert truth["dataset_id"] == manifest["dataset_id"]
    assert len(truth["incident_definitions"]) == 2


# ---- Data quality rules -------------------------------------------------------

def test_failure_reason_present_only_for_failed_payments():
    t = small_data().transactions
    failed = t["status"] == "FAILED"
    assert t.loc[failed, "failure_reason"].notna().all()
    assert t.loc[~failed, "failure_reason"].isna().all()


def test_basic_column_rules():
    t = small_data().transactions
    assert t["transaction_id"].is_unique
    assert t["created_at"].is_monotonic_increasing
    assert (t["amount"] > 0).all()
    assert (t["latency_ms"] >= 0).all()
    assert set(t["currency"]) == {"USD"}
    assert set(t["status"]) == {"SUCCEEDED", "FAILED"}
    assert set(t["provider_code"]) <= set(small_data().providers["provider_code"])


def test_all_timestamps_inside_the_window():
    t = small_data().transactions
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert t["created_at"].min() >= pd.Timestamp(start)
    assert t["created_at"].max() < pd.Timestamp(start + timedelta(days=SMALL.days))


def test_volume_is_close_to_configured_level():
    t = small_data().transactions
    per_day = len(t) / SMALL.days
    assert 1_400 < per_day < 1_800  # base 1,500 plus growth and weekday effects


def test_identifiers_are_synthetic():
    t = small_data().transactions
    assert t["customer_id"].str.fullmatch(r"CUST-\d{6}").all()
    assert t["transaction_id"].str.fullmatch(r"TXN-\d{8}").all()


# ---- The injected incident --------------------------------------------------

def test_primary_incident_raises_failures_only_at_the_affected_provider():
    t = small_data().transactions
    inc = _primary()
    in_window = (t["created_at"] >= _ts(inc["actual_start"])) & (t["created_at"] < _ts(inc["actual_end"]))
    failed = t["status"] == "FAILED"

    for provider, group in t[in_window].groupby("provider_code"):
        rate_in = failed[group.index].mean()
        rate_out = failed[(t["provider_code"] == provider) & ~in_window].mean()
        if provider == inc["provider_code"]:
            assert rate_in > 3 * rate_out, f"{provider}: {rate_in:.3f} vs {rate_out:.3f}"
        else:
            assert rate_in < 2 * rate_out, f"{provider} should be unaffected"


def test_extra_failures_use_the_incident_reason():
    t = small_data().transactions
    inc = _primary()
    window = (
        (t["created_at"] >= _ts(inc["actual_start"]))
        & (t["created_at"] < _ts(inc["actual_end"]))
        & (t["provider_code"] == inc["provider_code"])
        & (t["status"] == "FAILED")
    )
    assert t.loc[window, "failure_reason"].value_counts().idxmax() == inc["failure_reason"]
    assert inc["injected_failures"] > 50


def test_provider_reference_appears_only_after_the_notice_was_published():
    t = small_data().transactions
    inc = _primary()
    tagged = t[t["provider_event_id"] == inc["provider_incident_id"]]
    assert len(tagged) > 0
    assert (tagged["created_at"] >= _ts(inc["provider_reported_start"])).all()
    assert (tagged["status"] == "FAILED").all()
    # The problem started BEFORE the notice: early failures carry no reference.
    early = t[
        (t["created_at"] >= _ts(inc["actual_start"]))
        & (t["created_at"] < _ts(inc["provider_reported_start"]))
        & (t["provider_code"] == inc["provider_code"])
        & (t["failure_reason"] == inc["failure_reason"])
    ]
    assert len(early) > 0 and early["provider_event_id"].isna().all()


def test_secondary_incident_is_injected():
    secondary = next(i for i in small_data().ground_truth["injected_incidents"] if i["key"] == "secondary")
    assert secondary["injected_failures"] > 0


# ---- Deployments ------------------------------------------------------------

def test_scenario_deployments_exist_and_are_placed_correctly():
    data = small_data()
    d = data.deployments.set_index("deployment_id")
    truth = data.ground_truth["deployments"]
    correlated = d.loc[truth["temporally_correlated"]]
    herring = d.loc[truth["red_herring"]]
    start = _ts(_primary()["actual_start"])
    assert _ts(correlated["deployed_at"]) == start - pd.Timedelta(hours=2)
    assert _ts(herring["deployed_at"]) < start - pd.Timedelta(days=1)


def test_no_routine_deployment_lands_inside_an_incident():
    data = small_data()
    scenario_ids = set(data.ground_truth["deployments"].values())
    routine = data.deployments[~data.deployments["deployment_id"].isin(scenario_ids)]
    for inc in data.ground_truth["injected_incidents"]:
        lo, hi = _ts(inc["actual_start"]), _ts(inc["actual_end"])
        times = routine["deployed_at"].map(_ts)
        assert not ((times >= lo) & (times <= hi)).any()
