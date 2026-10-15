"""Failure rates, period comparison, significance test, week boundaries, filters.
Expected values are worked out by hand in the comments."""

import math
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from pydantic import ValidationError

from payops.analytics.metrics import compare_periods, failure_rate, last_complete_week, two_proportion_test
from payops.analytics.models import AnalysisFilter

UTC = timezone.utc


def test_failure_rate_basic():
    assert failure_rate(5, 100) == 0.05


def test_failure_rate_with_no_attempts_is_undefined_not_zero():
    assert math.isnan(failure_rate(0, 0))


@pytest.mark.parametrize("failures, attempts", [(-1, 10), (11, 10), (1, -5)])
def test_failure_rate_rejects_impossible_counts(failures, attempts):
    with pytest.raises(ValueError):
        failure_rate(failures, attempts)


def test_two_proportion_test_matches_hand_calculation():
    # pooled p = 70/2000 = 0.035; se = sqrt(0.035*0.965*(2/1000)) = 0.0082189
    # z = (0.05 - 0.02) / 0.0082189 = 3.6501; p = erfc(3.6501/sqrt(2)) = 0.000262
    z, p = two_proportion_test([50], [1000], [20], [1000])
    assert z[0] == pytest.approx(3.6501320661951637)
    assert p[0] == pytest.approx(0.00026210551099920394)


def test_two_proportion_test_with_zero_attempts_gives_nan():
    z, p = two_proportion_test([0], [0], [5], [100])
    assert math.isnan(z[0]) and math.isnan(p[0])


def _frame(rows):
    return pd.DataFrame(rows, columns=["provider_code", "attempts", "failures"])


def test_compare_periods_hand_example():
    current = _frame([("A", 1000, 50), ("B", 1000, 20), ("C", 100, 10)])
    baseline = _frame([("A", 1000, 20), ("B", 1000, 20)])  # C is new this period
    out = compare_periods(current, baseline, key="provider_code").set_index("provider_code")

    # A: expected = 1000 x 0.02 = 20, excess = 30, change = +3.0 points
    assert out.loc["A", "expected_failures"] == pytest.approx(20)
    assert out.loc["A", "excess_failures"] == pytest.approx(30)
    assert out.loc["A", "change_pct_points"] == pytest.approx(3.0)
    # B: unchanged
    assert out.loc["B", "excess_failures"] == pytest.approx(0)
    # C: no baseline -> expected 0, so all 10 failures count as excess
    assert out.loc["C", "excess_failures"] == pytest.approx(10)
    assert math.isnan(out.loc["C", "rate_baseline"])
    # shares of the 40 positive excess failures: A 30/40, C 10/40
    assert out.loc["A", "contribution_share"] == pytest.approx(0.75)
    assert out.loc["C", "contribution_share"] == pytest.approx(0.25)
    assert out.loc["A", "significant"]
    assert not out.loc["B", "significant"]


def test_compare_periods_sorts_largest_excess_first():
    out = compare_periods(_frame([("A", 100, 1), ("B", 100, 9)]), _frame([("A", 100, 1), ("B", 100, 1)]), "provider_code")
    assert list(out["provider_code"]) == ["B", "A"]


def test_compare_periods_with_no_increase_has_zero_shares():
    out = compare_periods(_frame([("A", 100, 1)]), _frame([("A", 100, 5)]), "provider_code")
    assert out["contribution_share"].tolist() == [0.0]


@pytest.mark.parametrize(
    "as_of, expected_start",
    [
        (datetime(2026, 9, 29, 0, 0, tzinfo=UTC), datetime(2026, 9, 21, tzinfo=UTC)),    # Tuesday 00:00
        (datetime(2026, 9, 28, 0, 0, tzinfo=UTC), datetime(2026, 9, 21, tzinfo=UTC)),    # Monday 00:00 exactly
        (datetime(2026, 9, 27, 15, 0, tzinfo=UTC), datetime(2026, 9, 14, tzinfo=UTC)),   # Sunday afternoon: week not finished
    ],
)
def test_last_complete_week(as_of, expected_start):
    start, end = last_complete_week(as_of)
    assert start == expected_start
    assert end - start == timedelta(days=7)
    assert start.weekday() == 0


def test_last_complete_week_requires_timezone():
    with pytest.raises(ValueError):
        last_complete_week(datetime(2026, 9, 29))


# ---- AnalysisFilter validation ------------------------------------------------

START = datetime(2026, 9, 21, tzinfo=UTC)
END = datetime(2026, 9, 28, tzinfo=UTC)


def test_filter_normalises_codes():
    f = AnalysisFilter(start=START, end=END, provider_codes=("ORBIT", "KESTREL", "ORBIT"))
    assert f.provider_codes == ("KESTREL", "ORBIT")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start": datetime(2026, 9, 21), "end": END},                       # no timezone
        {"start": END, "end": START},                                       # end before start
        {"start": START, "end": START + timedelta(days=401)},               # too long
        {"start": START, "end": END, "provider_codes": ("KESTREL'; DROP TABLE x; --",)},
        {"start": START, "end": END, "payment_methods": ("card",)},          # lower case is not a code
    ],
)
def test_filter_rejects_bad_input(kwargs):
    with pytest.raises(ValidationError):
        AnalysisFilter(**kwargs)


def test_filter_shift_moves_both_ends():
    f = AnalysisFilter(start=START, end=END).shifted(timedelta(days=-7))
    assert (f.start, f.end) == (START - timedelta(days=7), END - timedelta(days=7))
