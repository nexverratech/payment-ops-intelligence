"""Short-term forecasts of daily payment volume and failure rate.

Methods are deliberately simple and explainable:

* Volume: linear regression on a trend + day-of-week (9 numbers in total),
  solved with numpy least squares. Prediction interval = +/- 1.96 x the
  standard deviation of past errors (an approximation; see limitations).
* Failure rate: the median of the last 28 daily rates, with the 10th-90th
  percentile of those days as the band. The median ignores a few bad incident
  days, which is what we want for "normal" operations.

Every forecast is checked against a naive baseline: "same weekday last week".
If the model does not beat it, the result says so. A model that cannot beat
the naive answer should not be trusted.

scikit-learn is not needed for a 9-parameter linear model, so it is not a
dependency; this can be swapped for a scikit-learn model later if needed.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from payops.analytics.models import ForecastPoint, ForecastResult

MIN_HISTORY_DAYS = 28
HOLDOUT_DAYS = 14


def forecast_daily(daily: pd.DataFrame, horizon_days: int = 7) -> tuple[ForecastResult, ForecastResult]:
    """daily: columns [date, attempts, failures], one row per complete day.

    Returns (volume forecast, failure-rate forecast).
    """
    if not 1 <= horizon_days <= 28:
        raise ValueError("horizon_days must be between 1 and 28")
    d = daily.sort_values("date").reset_index(drop=True)
    d["date"] = pd.to_datetime(d["date"]).dt.normalize()
    d["rate"] = d["failures"] / d["attempts"]

    if len(d) < MIN_HISTORY_DAYS:
        msg = f"Only {len(d)} days of history; at least {MIN_HISTORY_DAYS} are needed. No forecast produced."
        return (
            ForecastResult(target="attempts", status="insufficient_data", method=VOLUME_METHOD, horizon_days=horizon_days, limitations=[msg]),
            ForecastResult(target="failure_rate", status="insufficient_data", method=RATE_METHOD, horizon_days=horizon_days, limitations=[msg]),
        )

    future = pd.date_range(d["date"].iloc[-1] + timedelta(days=1), periods=horizon_days, freq="D")
    return _volume(d, future), _rate(d, future)


VOLUME_METHOD = "Linear regression: daily attempts ~ trend + day of week (numpy least squares)."
RATE_METHOD = "Median of the last 28 daily failure rates; band = 10th-90th percentile of those days."
COMMON_LIMITS = [
    "Assumes the near future behaves like the recent past; incidents, promotions or holidays are not predicted.",
    "Built on synthetic data.",
]


def _volume(d: pd.DataFrame, future: pd.DatetimeIndex) -> ForecastResult:
    limitations = list(COMMON_LIMITS) + ["Interval is +/- 1.96 x residual standard deviation; it ignores uncertainty in the fitted trend."]
    backtest = None
    beats = None
    if len(d) >= MIN_HISTORY_DAYS + HOLDOUT_DAYS:
        train, test = d.iloc[:-HOLDOUT_DAYS], d.iloc[-HOLDOUT_DAYS:]
        coef, _ = _fit(train)
        model_pred = _design(test["date"], train["date"].iloc[0]) @ coef
        naive_pred = _seasonal_naive(d, test.index, "attempts")
        backtest = {
            "model_mae": round(float(np.mean(np.abs(model_pred - test["attempts"]))), 1),
            "seasonal_naive_mae": round(float(np.mean(np.abs(naive_pred - test["attempts"]))), 1),
            "holdout_days": HOLDOUT_DAYS,
        }
        beats = backtest["model_mae"] < backtest["seasonal_naive_mae"]
    else:
        limitations.append(f"Fewer than {MIN_HISTORY_DAYS + HOLDOUT_DAYS} days: no backtest against the baseline was possible.")

    coef, resid_sd = _fit(d)
    pred = _design(pd.Series(future), d["date"].iloc[0]) @ coef
    points = [
        ForecastPoint(date=day.date().isoformat(), value=round(float(v), 0),
                      lower=round(float(max(v - 1.96 * resid_sd, 0)), 0), upper=round(float(v + 1.96 * resid_sd), 0))
        for day, v in zip(future, pred)
    ]
    return ForecastResult(target="attempts", status="ok", method=VOLUME_METHOD, horizon_days=len(future),
                          points=points, backtest=backtest, model_beats_baseline=beats, limitations=limitations)


def _rate(d: pd.DataFrame, future: pd.DatetimeIndex) -> ForecastResult:
    recent = d["rate"].iloc[-MIN_HISTORY_DAYS:]
    median, lo, hi = (float(recent.quantile(q)) for q in (0.5, 0.1, 0.9))
    backtest = None
    beats = None
    if len(d) >= MIN_HISTORY_DAYS + HOLDOUT_DAYS:
        test_idx = d.index[-HOLDOUT_DAYS:]
        train_median = float(d["rate"].iloc[-HOLDOUT_DAYS - MIN_HISTORY_DAYS:-HOLDOUT_DAYS].median())
        naive = _seasonal_naive(d, test_idx, "rate")
        actual = d.loc[test_idx, "rate"].to_numpy()
        backtest = {
            "model_mae_pct_points": round(float(np.mean(np.abs(train_median - actual))) * 100, 3),
            "seasonal_naive_mae_pct_points": round(float(np.mean(np.abs(naive - actual))) * 100, 3),
            "holdout_days": HOLDOUT_DAYS,
        }
        beats = backtest["model_mae_pct_points"] < backtest["seasonal_naive_mae_pct_points"]
    points = [ForecastPoint(date=day.date().isoformat(), value=round(median, 5), lower=round(lo, 5), upper=round(hi, 5)) for day in future]
    return ForecastResult(target="failure_rate", status="ok", method=RATE_METHOD, horizon_days=len(future), points=points,
                          backtest=backtest, model_beats_baseline=beats,
                          limitations=list(COMMON_LIMITS) + ["A constant 'normal' rate: it cannot anticipate a new incident."])


def _design(dates: pd.Series, origin: pd.Timestamp) -> np.ndarray:
    """Columns: intercept, days since origin, 6 weekday dummies (Monday is the reference)."""
    dates = pd.to_datetime(pd.Series(dates).reset_index(drop=True))
    t = (dates - origin).dt.days.to_numpy(dtype=float)
    dow = dates.dt.weekday.to_numpy()
    dummies = np.stack([(dow == k).astype(float) for k in range(1, 7)], axis=1)
    return np.column_stack([np.ones(len(dates)), t, dummies])


def _fit(d: pd.DataFrame) -> tuple[np.ndarray, float]:
    x = _design(d["date"], d["date"].iloc[0])
    y = d["attempts"].to_numpy(dtype=float)
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ coef
    dof = max(len(y) - x.shape[1], 1)
    return coef, float(np.sqrt((resid ** 2).sum() / dof))


def _seasonal_naive(d: pd.DataFrame, idx: pd.Index, column: str) -> np.ndarray:
    """Prediction for day i = actual value on day i - 7."""
    return d[column].to_numpy(dtype=float)[np.asarray(idx) - 7]
