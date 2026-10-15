"""Anomaly detection and event correlation on small hand-built series."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from payops.analytics.detection import _merge_runs, detect_anomalies
from payops.analytics.events import change_around, related_events
from payops.analytics.models import AnomalyWindow

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def flat_series(days=10, attempts=100, failures=3, segment="KESTREL", spikes=None):
    """Hourly series with a constant 3% failure rate, plus {hour_offset: failures} spikes."""
    rows = []
    for i in range(days * 24):
        f = (spikes or {}).get(i, failures)
        rows.append({"provider_code": segment, "hour": T0 + timedelta(hours=i), "attempts": attempts, "failures": f})
    return pd.DataFrame(rows)


DAY9_10H = 9 * 24 + 10


def test_four_bad_hours_become_one_window_with_hand_computed_numbers():
    spikes = {DAY9_10H + k: 20 for k in range(4)}
    [w] = detect_anomalies(flat_series(spikes=spikes))
    assert w.segment == "KESTREL"
    assert w.start == T0 + timedelta(hours=DAY9_10H)
    assert w.end == T0 + timedelta(hours=DAY9_10H + 4)
    assert w.flagged_hours == 4
    assert w.baseline_rate == pytest.approx(0.03)
    # expected = 400 attempts x 0.03 = 12; excess = 80 - 12 = 68
    assert w.expected_failures == pytest.approx(12)
    assert w.excess_failures == pytest.approx(68)
    # z = (20 - 3) / sqrt(100 x 0.03 x 0.97) = 9.97
    assert w.max_z == pytest.approx(9.97, abs=0.01)


def test_a_single_extreme_hour_is_not_reported():
    assert detect_anomalies(flat_series(spikes={DAY9_10H: 25})) == []


def test_nothing_is_flagged_before_there_is_a_baseline():
    # Day 2 is inside the first 8 days: no 7-day baseline ending 24 h earlier exists yet.
    spikes = {2 * 24 + 10 + k: 30 for k in range(5)}
    assert detect_anomalies(flat_series(spikes=spikes)) == []


def test_steady_series_has_no_anomalies():
    assert detect_anomalies(flat_series()) == []


def test_bad_hours_separated_by_a_short_quiet_gap_are_merged():
    spikes = {DAY9_10H: 20, DAY9_10H + 1: 20, DAY9_10H + 5: 20, DAY9_10H + 6: 20}
    [w] = detect_anomalies(flat_series(spikes=spikes))
    assert w.end - w.start == timedelta(hours=7)
    assert w.flagged_hours == 4


def test_segments_are_checked_independently():
    bad = flat_series(spikes={DAY9_10H + k: 20 for k in range(3)})
    good = flat_series(segment="NOVAPAY")
    [w] = detect_anomalies(pd.concat([bad, good]))
    assert w.segment == "KESTREL"


def test_merge_runs():
    h = [T0 + timedelta(hours=x) for x in (0, 1, 5, 20)]
    runs = _merge_runs(h, merge_gap_hours=6)
    assert runs == [(T0, T0 + timedelta(hours=6)), (T0 + timedelta(hours=20), T0 + timedelta(hours=21))]


# ---- Events -----------------------------------------------------------------

WINDOW = AnomalyWindow(
    segment="KESTREL", start=datetime(2026, 9, 23, 14, tzinfo=timezone.utc), end=datetime(2026, 9, 25, 1, tzinfo=timezone.utc),
    attempts=2000, failures=500, expected_failures=60, excess_failures=440, baseline_rate=0.03,
    window_rate=0.25, peak_hour_rate=0.34, max_z=16.0, flagged_hours=29,
)
DEPLOYMENTS = pd.DataFrame([
    {"deployment_id": "DEP-1", "service": "checkout-service", "version": "v2.10", "deployed_at": "2026-09-23T12:00:00Z", "change_summary": "x"},
    {"deployment_id": "DEP-2", "service": "fraud-rules", "version": "v5.5", "deployed_at": "2026-09-20T10:00:00Z", "change_summary": "y"},
    {"deployment_id": "DEP-3", "service": "ledger-service", "version": "v1.2", "deployed_at": "2026-09-23T19:00:00Z", "change_summary": "z"},
])
NOTICES = pd.DataFrame([
    {"incident_id": "INC-3", "provider_code": "KESTREL", "reported_start_at": "2026-09-23T17:00:00Z", "reported_end_at": "2026-09-25T04:30:00Z", "title": "degraded"},
    {"incident_id": "INC-1", "provider_code": "BLUERIVER", "reported_start_at": "2026-08-10T02:00:00Z", "reported_end_at": "2026-08-10T03:00:00Z", "title": "maintenance"},
])


def test_related_events_selects_by_time_and_measures_offsets():
    events = {e.event_id: e for e in related_events(WINDOW, DEPLOYMENTS, NOTICES, lookback_hours=24)}
    assert set(events) == {"DEP-1", "DEP-3", "INC-3"}           # DEP-2 and INC-1 are too early
    assert events["DEP-1"].hours_from_anomaly_start == pytest.approx(-2.0)
    assert events["DEP-3"].hours_from_anomaly_start == pytest.approx(5.0)
    assert events["INC-3"].hours_from_anomaly_start == pytest.approx(3.0)
    assert events["INC-3"].same_provider is True


def test_every_relationship_says_it_is_not_proof_of_cause():
    for e in related_events(WINDOW, DEPLOYMENTS, NOTICES):
        assert "not evidence of cause" in e.relationship


def test_change_around_finds_the_one_provider_that_got_worse():
    at = T0 + timedelta(days=5)
    rows = []
    for provider, after_failures in (("KESTREL", 20), ("NOVAPAY", 3)):
        for i in range(-24, 24):
            rows.append({"provider_code": provider, "hour": at + timedelta(hours=i), "attempts": 100,
                         "failures": after_failures if i >= 0 else 3})
    out = change_around(pd.DataFrame(rows), at, hours=24).set_index("provider_code")
    assert out.loc["KESTREL", "rate_before"] == pytest.approx(0.03)
    assert out.loc["KESTREL", "rate_after"] == pytest.approx(0.20)
    assert out.loc["KESTREL", "significant_increase"]
    assert not out.loc["NOVAPAY", "significant_increase"]
    assert out.loc["ALL", "attempts_after"] == 4800
