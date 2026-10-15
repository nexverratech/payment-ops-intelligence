"""Business impact (measured vs estimate vs hypothetical) and forecasting."""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from payops.analytics.forecast import forecast_daily
from payops.analytics.impact import ImpactAssumptions, window_impact


def figures(**overrides):
    args = dict(attempts=1000, failures=100, failed_value=5000.0, expected_failures=30.0, duration_hours=10)
    args.update(overrides)
    return {f.name: f for f in window_impact(**args)}


def test_impact_hand_example():
    f = figures()
    # excess = 100 - 30 = 70; average failed amount = 5000 / 100 = 50
    assert f["failed_payments"].value == 100
    assert f["affected_transaction_value"].value == 5000.0
    assert f["excess_failures"].value == 70
    assert f["excess_affected_value"].value == 3500.0          # 70 x 50
    assert f["excess_failures_per_hour"].value == 7.0          # 70 / 10 h
    assert f["projected_additional_failures"].value == 168.0   # 7 x 24 h
    assert f["handling_cost_low"].value == 17.5                 # 70 x 0.25
    assert f["handling_cost_base"].value == 70.0                # 70 x 1.00
    assert f["handling_cost_high"].value == 210.0               # 70 x 3.00


def test_every_figure_is_labelled_with_its_kind():
    f = figures()
    assert f["failed_payments"].kind == "measured"
    assert f["affected_transaction_value"].kind == "measured"
    assert f["excess_failures"].kind == "estimate"
    assert f["projected_additional_failures"].kind == "hypothetical"
    assert all(x.kind == "hypothetical" for name, x in f.items() if name.startswith("handling_cost"))
    assert "not revenue or loss" in f["affected_transaction_value"].method


def test_negative_excess_does_not_create_negative_costs():
    f = figures(failures=20, expected_failures=30.0, failed_value=1000.0)
    assert f["excess_failures"].value == -10
    assert f["excess_affected_value"].value == 0
    assert f["handling_cost_high"].value == 0


def test_custom_assumptions_are_used():
    f = {x.name: x for x in window_impact(attempts=10, failures=5, failed_value=50, expected_failures=1, duration_hours=2,
                                          assumptions=ImpactAssumptions(cost_per_failure_scenarios={"only": 2.0}, projection_hours=4))}
    assert f["handling_cost_only"].value == 8.0           # 4 excess x 2.00
    assert f["projected_additional_failures"].value == 8.0  # 2/h x 4 h


@pytest.mark.parametrize("bad", [dict(duration_hours=0), dict(failures=2000), dict(failures=-1)])
def test_impact_rejects_impossible_input(bad):
    with pytest.raises(ValueError):
        figures(**bad)


# ---- Forecast -----------------------------------------------------------------

WEEKLY = [0, 50, 60, 40, 30, 120, -80]  # Monday ... Sunday offsets


def series(days, start=date(2026, 7, 6), rate=0.03):  # 6 Jul 2026 is a Monday
    rows = []
    for t in range(days):
        d = start + timedelta(days=t)
        attempts = 1000 + 10 * t + WEEKLY[d.weekday()]
        rows.append({"date": pd.Timestamp(d), "attempts": attempts, "failures": round(attempts * rate)})
    return pd.DataFrame(rows)


def test_refuses_to_forecast_with_too_little_history():
    volume, rate = forecast_daily(series(20))
    assert volume.status == rate.status == "insufficient_data"
    assert volume.points == [] and "at least 28" in volume.limitations[0]


def test_volume_model_recovers_a_perfect_trend_plus_weekday_pattern():
    volume, _ = forecast_daily(series(60), horizon_days=7)
    expected_next = series(61)["attempts"].iloc[-1]        # the true value on day 61
    assert volume.points[0].value == pytest.approx(expected_next, abs=1)
    assert volume.points[0].upper - volume.points[0].lower < 2  # no noise -> tiny interval
    assert len(volume.points) == 7


def test_volume_model_beats_naive_baseline_on_trending_data():
    volume, _ = forecast_daily(series(60))
    # The naive "same day last week" is always 7 days x 10 = 70 payments behind the trend.
    assert volume.backtest["seasonal_naive_mae"] == pytest.approx(70, abs=0.5)
    assert volume.backtest["model_mae"] < 1
    assert volume.model_beats_baseline is True


def test_rate_forecast_is_the_recent_median():
    _, rate = forecast_daily(series(60, rate=0.03))
    assert rate.points[0].value == pytest.approx(0.03, abs=0.0005)


def test_backtest_skipped_but_forecast_given_with_28_to_41_days():
    volume, _ = forecast_daily(series(30))
    assert volume.status == "ok" and volume.backtest is None
    assert any("no backtest" in x for x in volume.limitations)


def test_horizon_is_validated():
    with pytest.raises(ValueError):
        forecast_daily(series(60), horizon_days=0)


def test_forecast_ignores_row_order():
    a, _ = forecast_daily(series(60))
    b, _ = forecast_daily(series(60).sample(frac=1, random_state=np.random.default_rng(1).integers(1e6)))
    assert a.points == b.points
