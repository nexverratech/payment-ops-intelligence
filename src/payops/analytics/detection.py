"""Baseline incident detection: transparent statistics, no machine learning.

For every hour and every segment (e.g. provider):

1. Baseline failure rate = the segment's failures / attempts over the 7 days
   that ended 24 hours earlier. The 24-hour gap stops a long incident from
   quickly "learning" its own bad hours as normal.
2. Expected failures this hour = attempts x baseline rate.
3. z-score = (actual - expected) / standard deviation of a binomial count.
   A z of 4 means "4 standard deviations above normal": very unlikely by chance.
4. An hour is flagged when z >= 4 AND there are at least 5 more failures
   than expected (so a quiet night hour with 2 failures instead of 0.4 is ignored).
5. Flagged hours less than 6 hours apart are merged into one anomaly window,
   because traffic is thin at night and an incident can look like it "pauses".
6. A window is reported only if it has at least 2 flagged hours and at least
   15 excess failures in total. Checking ~2,000 hours per provider means a few
   single hours will look extreme purely by chance; this rule removes them.

Limits, stated honestly: the first 8 days of data cannot be checked (no
baseline yet); a real problem lasting under 2 hours or causing fewer than
15 extra failures is not reported; a slow, gradual rise may never cross the threshold; a problem
that affects every provider equally is caught only on the "ALL" segment.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from payops.analytics.models import AnomalyWindow

DEFAULTS = {
    "baseline_hours": 168,
    "gap_before_hours": 24,
    "min_baseline_attempts": 500,
    "z_threshold": 4.0,
    "min_excess": 5.0,
    "merge_gap_hours": 6,
    "min_window_flagged_hours": 2,
    "min_window_excess": 15.0,
}


def score_hours(
    hourly: pd.DataFrame,
    *,
    baseline_hours: int = DEFAULTS["baseline_hours"],
    gap_before_hours: int = DEFAULTS["gap_before_hours"],
    min_baseline_attempts: int = DEFAULTS["min_baseline_attempts"],
) -> pd.DataFrame:
    """Add baseline rate, expected failures and z-score to ONE segment's hourly series.

    Input columns: hour (tz-aware), attempts, failures. Missing hours are filled with zeros.
    """
    s = hourly.set_index("hour")[["attempts", "failures"]].sort_index()
    full = pd.date_range(s.index.min(), s.index.max(), freq="h")
    s = s.reindex(full, fill_value=0)
    s.index.name = "hour"

    window = dict(window=baseline_hours, min_periods=baseline_hours)
    base_attempts = s["attempts"].rolling(**window).sum().shift(gap_before_hours)
    base_failures = s["failures"].rolling(**window).sum().shift(gap_before_hours)
    s["baseline_rate"] = (base_failures / base_attempts).where(base_attempts >= min_baseline_attempts)

    s["expected"] = s["attempts"] * s["baseline_rate"]
    sd = np.sqrt(s["attempts"] * s["baseline_rate"] * (1 - s["baseline_rate"]))
    s["z"] = ((s["failures"] - s["expected"]) / sd).where(sd > 0)
    return s.reset_index()


def detect_anomalies(
    hourly: pd.DataFrame,
    segment_col: str = "provider_code",
    *,
    z_threshold: float = DEFAULTS["z_threshold"],
    min_excess: float = DEFAULTS["min_excess"],
    merge_gap_hours: int = DEFAULTS["merge_gap_hours"],
    min_window_flagged_hours: int = DEFAULTS["min_window_flagged_hours"],
    min_window_excess: float = DEFAULTS["min_window_excess"],
    **score_options,
) -> list[AnomalyWindow]:
    """Find anomaly windows in an hourly table with columns [segment_col, hour, attempts, failures].

    Returns windows sorted by excess failures, largest first.
    """
    windows: list[AnomalyWindow] = []
    for segment, group in hourly.groupby(segment_col):
        scored = score_hours(group, **score_options)
        flagged = scored[(scored["z"] >= z_threshold) & (scored["failures"] - scored["expected"] >= min_excess)]
        for start, end in _merge_runs(list(flagged["hour"]), merge_gap_hours):
            window = _summarise(str(segment), scored, flagged, start, end)
            if window.flagged_hours >= min_window_flagged_hours and window.excess_failures >= min_window_excess:
                windows.append(window)
    return sorted(windows, key=lambda w: w.excess_failures, reverse=True)


def _merge_runs(hours: list[pd.Timestamp], merge_gap_hours: int) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """[10:00, 11:00, 15:00] with gap 6 -> [(10:00, 16:00)]; end is exclusive."""
    runs: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for h in sorted(hours):
        if runs and h - (runs[-1][1] - timedelta(hours=1)) <= timedelta(hours=merge_gap_hours):
            runs[-1] = (runs[-1][0], h + timedelta(hours=1))
        else:
            runs.append((h, h + timedelta(hours=1)))
    return runs


def _summarise(segment: str, scored: pd.DataFrame, flagged: pd.DataFrame, start, end) -> AnomalyWindow:
    inside = scored[(scored["hour"] >= start) & (scored["hour"] < end)]
    attempts = int(inside["attempts"].sum())
    failures = int(inside["failures"].sum())
    # Freeze the baseline at the start of the window: "normal" as it was before things went wrong.
    baseline_rate = float(inside["baseline_rate"].iloc[0])
    expected = attempts * baseline_rate
    busy = inside[inside["attempts"] >= 20]
    peak_source = busy if len(busy) else inside
    in_flag = flagged[(flagged["hour"] >= start) & (flagged["hour"] < end)]
    return AnomalyWindow(
        segment=segment,
        start=start.to_pydatetime(),
        end=end.to_pydatetime(),
        attempts=attempts,
        failures=failures,
        expected_failures=round(expected, 1),
        excess_failures=round(failures - expected, 1),
        baseline_rate=round(baseline_rate, 5),
        window_rate=round(failures / attempts, 5) if attempts else 0.0,
        peak_hour_rate=round(float((peak_source["failures"] / peak_source["attempts"].where(peak_source["attempts"] > 0)).max()), 5),
        max_z=round(float(in_flag["z"].max()), 2),
        flagged_hours=len(in_flag),
    )
