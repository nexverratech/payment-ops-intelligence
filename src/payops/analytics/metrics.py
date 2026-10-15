"""Failure rates and period-over-period comparison.

Key ideas, in plain words:

* failure rate = failed payments / payment attempts
* expected failures = attempts this period x last period's failure rate
  ("how many would have failed if nothing had changed")
* excess failures = actual failures - expected failures
* contribution share = this segment's excess / total excess of all segments
  that got worse. "KESTREL explains 92% of the extra failures."
* a two-proportion z-test says whether a rate change is bigger than normal
  random variation. It does NOT say why the change happened.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd


def failure_rate(failures: float, attempts: float) -> float:
    """Failures / attempts. Returns NaN when there were no attempts (rate is undefined, not 0)."""
    if attempts < 0 or failures < 0 or failures > attempts:
        raise ValueError(f"invalid counts: failures={failures}, attempts={attempts}")
    return failures / attempts if attempts else float("nan")


def two_proportion_test(f1: np.ndarray, n1: np.ndarray, f2: np.ndarray, n2: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """z-score and two-sided p-value for "rate 1 differs from rate 2".

    Uses the pooled normal approximation. Rows with no attempts get NaN.
    """
    f1, n1, f2, n2 = (np.asarray(a, dtype=float) for a in (f1, n1, f2, n2))
    with np.errstate(divide="ignore", invalid="ignore"):
        p1, p2 = f1 / n1, f2 / n2
        pooled = (f1 + f2) / (n1 + n2)
        se = np.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
        z = np.where(se > 0, (p1 - p2) / se, np.nan)
    p = np.array([math.erfc(abs(v) / math.sqrt(2)) if np.isfinite(v) else np.nan for v in np.atleast_1d(z)])
    return z, p


def compare_periods(current: pd.DataFrame, baseline: pd.DataFrame, key: str, significance: float = 0.01) -> pd.DataFrame:
    """Compare two periods segment by segment (e.g. per provider).

    Both inputs need columns [key, attempts, failures]. Output is sorted with
    the largest excess failures first.
    """
    cols = [key, "attempts", "failures"]
    cur = current[cols].set_index(key)
    base = baseline[cols].set_index(key)
    df = cur.join(base, how="outer", lsuffix="_current", rsuffix="_baseline").fillna(0)
    df = df.astype({c: "int64" for c in df.columns})

    with np.errstate(divide="ignore", invalid="ignore"):
        df["rate_current"] = df["failures_current"] / df["attempts_current"]
        df["rate_baseline"] = df["failures_baseline"] / df["attempts_baseline"]
    df["change_pct_points"] = (df["rate_current"] - df["rate_baseline"]) * 100
    df["expected_failures"] = df["attempts_current"] * df["rate_baseline"].fillna(0)
    df["excess_failures"] = df["failures_current"] - df["expected_failures"]

    positive = df["excess_failures"].clip(lower=0)
    total = positive.sum()
    df["contribution_share"] = positive / total if total > 0 else 0.0

    z, p = two_proportion_test(
        df["failures_current"], df["attempts_current"], df["failures_baseline"], df["attempts_baseline"]
    )
    df["z_score"] = z
    df["p_value"] = p
    df["significant"] = df["p_value"] < significance

    return df.sort_values("excess_failures", ascending=False).reset_index()


def last_complete_week(as_of: datetime) -> tuple[datetime, datetime]:
    """The last full Monday 00:00 -> Monday 00:00 week that ended on or before `as_of`."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    midnight = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = midnight - timedelta(days=midnight.weekday())
    return week_end - timedelta(days=7), week_end
