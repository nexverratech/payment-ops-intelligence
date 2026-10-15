"""Read-only analytics endpoints used by the dashboard.

Every endpoint: validated filter -> parameterized SQL on the read-only account
-> deterministic calculation -> JSON. Comparisons always use the previous period
of the same length (a 7-day range is compared with the 7 days before it).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Depends, Query

from payops.analytics import queries
from payops.analytics.detection import detect_anomalies
from payops.analytics.events import related_events
from payops.analytics.metrics import compare_periods
from payops.analytics.models import FAILURE_REASONS, PAYMENT_METHODS, AnalysisFilter, Figure
from payops.analytics.serialize import to_records
from payops.api.auth import require
from payops.api.deps import Filter, ReadOnlyConn
from payops.api.schemas import KpiResponse, Period, TableResponse
from payops.security.rbac import Permission

# Every endpoint here needs a signed-in user with analytics:read (all four roles have it).
router = APIRouter(prefix="/api/v1", tags=["analytics"], dependencies=[Depends(require(Permission.ANALYTICS_READ))])

DETECTION_HISTORY = timedelta(days=9)


def _previous(f: AnalysisFilter) -> AnalysisFilter:
    return f.shifted(-(f.end - f.start))


def _period(f: AnalysisFilter) -> Period:
    return Period(start=f.start, end=f.end)


@router.get("/meta")
def meta(conn: ReadOnlyConn) -> dict:
    """What is loaded and which filter values exist."""
    window = queries.dataset_window(conn)
    return {
        "dataset_id": window["dataset_id"],
        "window_start": window["window_start"],
        "window_end": window["window_end"],
        "providers": to_records(queries.providers(conn)),
        "payment_methods": list(PAYMENT_METHODS),
        "failure_reasons": list(FAILURE_REASONS),
    }


@router.get("/kpis", response_model=KpiResponse)
def kpis(conn: ReadOnlyConn, f: Filter) -> KpiResponse:
    prev = _previous(f)
    cur, old = queries.summary(conn, f), queries.summary(conn, prev)
    rate = cur["failures"] / cur["attempts"] if cur["attempts"] else None
    old_rate = old["failures"] / old["attempts"] if old["attempts"] else None
    figures = [
        Figure(name="attempts", value=cur["attempts"], unit="payments", kind="measured", method="Count of payment attempts."),
        Figure(name="succeeded", value=cur["attempts"] - cur["failures"], unit="payments", kind="measured",
               method="Attempts minus failures (with a reason filter: minus failures of those reasons)."),
        Figure(name="failures", value=cur["failures"], unit="payments", kind="measured", method="Count of FAILED payments."),
        Figure(name="affected_transaction_value", value=round(cur["failed_value"], 2), unit="USD", kind="measured",
               method="Sum of failed payment amounts. Transaction value, not revenue or loss."),
        Figure(name="previous_failures", value=old["failures"], unit="payments", kind="measured", method="Failures in the previous period."),
        Figure(name="previous_affected_transaction_value", value=round(old["failed_value"], 2), unit="USD", kind="measured",
               method="Same, for the previous period of equal length."),
    ]
    if rate is not None:
        figures.append(Figure(name="failure_rate", value=round(rate * 100, 3), unit="%", kind="measured", method="Failures / attempts."))
    if old_rate is not None:
        figures.append(Figure(name="previous_failure_rate", value=round(old_rate * 100, 3), unit="%", kind="measured",
                              method="Failures / attempts in the previous period of equal length."))
    return KpiResponse(current=_period(f), previous=_period(prev), figures=figures)


@router.get("/timeseries", response_model=TableResponse)
def timeseries(
    conn: ReadOnlyConn, f: Filter,
    unit: Annotated[Literal["hour", "3h", "day"], Query()] = "day",
    by_provider: Annotated[bool, Query()] = True,
) -> TableResponse:
    df = queries.time_series(conn, f, unit, by_provider)
    if not df.empty:
        df["failure_rate"] = df["failures"] / df["attempts"]
        df = df.rename(columns={"hour": "bucket", "day": "bucket"})
    return TableResponse(current=_period(f), rows=to_records(df))


@router.get("/breakdown", response_model=TableResponse)
def breakdown(conn: ReadOnlyConn, f: Filter, by: Annotated[Literal["provider", "method"], Query()] = "provider") -> TableResponse:
    prev = _previous(f)
    key = queries.DIMENSIONS[by]
    cur_df, prev_df = queries.breakdown(conn, f, by), queries.breakdown(conn, prev, by)
    if cur_df.empty:
        return TableResponse(current=_period(f), previous=_period(prev), rows=[])
    if prev_df.empty:
        prev_df = pd.DataFrame(columns=[key, "attempts", "failures"])
    table = compare_periods(cur_df, prev_df, key=key)
    values = cur_df[[key, "total_value", "failed_value"]]
    return TableResponse(current=_period(f), previous=_period(prev), rows=to_records(table.merge(values, on=key, how="left")))


@router.get("/failure-reasons", response_model=TableResponse)
def failure_reasons(conn: ReadOnlyConn, f: Filter) -> TableResponse:
    prev = _previous(f)
    cur = queries.failure_reasons(conn, f)
    old = queries.failure_reasons(conn, prev)
    cur = cur if not cur.empty else pd.DataFrame(columns=["failure_reason", "failures", "failed_value"])
    old = old if not old.empty else pd.DataFrame(columns=["failure_reason", "failures", "failed_value"])
    table = cur.merge(old[["failure_reason", "failures"]], on="failure_reason", how="outer", suffixes=("", "_previous")).fillna(0)
    table["change"] = table["failures"] - table["failures_previous"]
    return TableResponse(current=_period(f), previous=_period(prev), rows=to_records(table.sort_values("failures", ascending=False)))


@router.get("/events", response_model=TableResponse)
def events(conn: ReadOnlyConn, f: Filter) -> TableResponse:
    deployments, notices = queries.events(conn, f.start, f.end)
    rows = [{"kind": "deployment", **r} for r in to_records(deployments)]
    rows += [{"kind": "provider_notice", **r} for r in to_records(notices)]
    return TableResponse(current=_period(f), rows=rows)


@router.get("/anomalies", response_model=TableResponse)
def anomalies(conn: ReadOnlyConn, f: Filter) -> TableResponse:
    """Anomaly windows that overlap the range, with nearby deployments and provider notices.

    Detection needs ~9 days of history before the range to build its baseline.
    """
    window = queries.dataset_window(conn)
    history = f.model_copy(update={"start": max(f.start - DETECTION_HISTORY, window["window_start"])})
    hourly = queries.time_series(conn, history, "hour", by_provider=True)
    if hourly.empty:
        return TableResponse(current=_period(f), rows=[])
    deployments, notices = queries.events(conn, history.start, f.end)
    rows = []
    for w in detect_anomalies(hourly):
        if w.start < f.end and w.end > f.start:
            rows.append({**w.model_dump(mode="json"),
                         "related_events": [e.model_dump(mode="json") for e in related_events(w, deployments, notices)]})
    return TableResponse(current=_period(f), rows=rows)
