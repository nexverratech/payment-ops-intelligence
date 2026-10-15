"""Canned data shared by API and dashboard tests (no database needed)."""

from datetime import datetime, timedelta, timezone

import pandas as pd

WINDOW = {
    "dataset_id": "test-dataset",
    "window_start": datetime(2026, 7, 1, tzinfo=timezone.utc),
    "window_end": datetime(2026, 9, 29, tzinfo=timezone.utc),
}
LAST_WEEK = (datetime(2026, 9, 21, tzinfo=timezone.utc), datetime(2026, 9, 28, tzinfo=timezone.utc))


SPIKE_AT = LAST_WEEK[0] + timedelta(days=2, hours=14)


def summary(conn, f):
    """Whole periods: 1,000 attempts; 50 failures in the 'current' week, 20 in any earlier period.
    Short windows (under 2 days, e.g. an anomaly window) are computed from the same hourly
    series the anomaly detection sees, so recomputation checks agree."""
    if f.end - f.start < timedelta(days=2):
        h = hourly(f.start, days=2, spike_at=SPIKE_AT)
        h = h[(h["hour"] >= f.start) & (h["hour"] < f.end)]
        return {"attempts": int(h["attempts"].sum()), "failures": int(h["failures"].sum()),
                "total_value": 0.0, "failed_value": float(h["failures"].sum()) * 50.0}
    current = f.start >= LAST_WEEK[0]
    return {"attempts": 1000, "failures": 50 if current else 20, "total_value": 50_000.0,
            "failed_value": 2_500.0 if current else 1_000.0}


def breakdown(conn, f, by):
    current = f.start >= LAST_WEEK[0]
    key = {"provider": "provider_code", "method": "payment_method"}[by]
    return pd.DataFrame([
        {key: "KESTREL", "attempts": 500, "failures": 40 if current else 10, "total_value": 25_000.0, "failed_value": 2_000.0},
        {key: "NOVAPAY", "attempts": 500, "failures": 10, "total_value": 25_000.0, "failed_value": 500.0},
    ])


def failure_reasons(conn, f):
    current = f.start >= LAST_WEEK[0]
    return pd.DataFrame([
        {"failure_reason": "TIMEOUT", "failures": 35 if current else 5, "failed_value": 1_750.0},
        {"failure_reason": "INSUFFICIENT_FUNDS", "failures": 15, "failed_value": 750.0},
    ])


def hourly(start: datetime, days: int = 18, spike_at: datetime | None = None, segment: str = "KESTREL") -> pd.DataFrame:
    """Flat 3% failure rate; 4 hours at 20% starting at `spike_at`."""
    rows = []
    for i in range(days * 24):
        h = start + timedelta(hours=i)
        bad = spike_at is not None and spike_at <= h < spike_at + timedelta(hours=4)
        rows.append({"provider_code": segment, "hour": h, "attempts": 100, "failures": 20 if bad else 3, "failed_value": 0.0})
    return pd.DataFrame(rows)


def time_series(conn, f, unit, by_provider):
    df = hourly(f.start, days=max(1, (f.end - f.start).days), spike_at=SPIKE_AT)
    df = df[(df["hour"] >= f.start) & (df["hour"] < f.end)]
    if unit == "day":
        df = df.assign(day=df["hour"].dt.floor("D")).groupby((["provider_code"] if by_provider else []) + ["day"],
                                                             as_index=False)[["attempts", "failures", "failed_value"]].sum()
        return df
    return df if by_provider else df.drop(columns="provider_code")


def events(conn, start, end):
    deployments = pd.DataFrame([{"deployment_id": "DEP-1", "service": "checkout-service", "version": "v2.10",
                                 "deployed_at": LAST_WEEK[0] + timedelta(days=2, hours=12), "team": "web",
                                 "change_summary": "retry header tweak"}])
    notices = pd.DataFrame([{"incident_id": "INC-3", "provider_code": "KESTREL",
                             "reported_start_at": LAST_WEEK[0] + timedelta(days=2, hours=17),
                             "reported_end_at": LAST_WEEK[0] + timedelta(days=3), "severity": "major", "title": "degraded"}])
    return deployments, notices


def sample_failed_transactions(conn, f, limit=5):
    return [f"TXN-{i:08d}" for i in range(1, min(limit, 3) + 1)]


def providers(conn):
    return pd.DataFrame([{"provider_code": "KESTREL", "display_name": "Kestrel Gateway"},
                         {"provider_code": "NOVAPAY", "display_name": "NovaPay"}])


def install(monkeypatch):
    """Replace every SQL query function with the canned versions above."""
    from payops.analytics import queries

    monkeypatch.setattr(queries, "dataset_window", lambda conn: dict(WINDOW))
    for name in ("summary", "breakdown", "failure_reasons", "time_series", "events", "providers", "sample_failed_transactions"):
        monkeypatch.setattr(queries, name, globals()[name])
