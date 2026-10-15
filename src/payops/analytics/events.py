"""Relate an anomaly to deployments and provider notices by TIME.

A deployment shortly before an anomaly is worth investigating, but timing alone
never proves cause. Every relationship text here says so.

`change_around` adds a useful extra clue: if failures rose at ONE provider only
after our own deployment, a provider-side problem is the more likely
explanation, because our software is shared by all providers. That is still a
hypothesis, and the report must say so.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from payops.analytics.metrics import two_proportion_test
from payops.analytics.models import AnomalyWindow, RelatedEvent

CORRELATION_NOTE = "Timing overlap only; this is not evidence of cause."


def related_events(
    window: AnomalyWindow,
    deployments: pd.DataFrame,
    notices: pd.DataFrame,
    lookback_hours: float = 24,
) -> list[RelatedEvent]:
    """Deployments and provider notices from `lookback_hours` before the window until its end.

    deployments: deployment_id, service, version, deployed_at, change_summary
    notices:     incident_id, provider_code, reported_start_at, reported_end_at, title
    """
    since = window.start - timedelta(hours=lookback_hours)
    events: list[RelatedEvent] = []

    for d in deployments.itertuples(index=False):
        at = _ts(d.deployed_at)
        if since <= at < window.end:
            hours = _hours(at, window.start)
            when = f"{abs(hours):.1f} h before the anomaly began" if hours < 0 else f"{hours:.1f} h after the anomaly began"
            events.append(RelatedEvent(
                kind="deployment",
                event_id=d.deployment_id,
                at=at,
                hours_from_anomaly_start=round(hours, 2),
                description=f"{d.service} {d.version}: {d.change_summary}",
                relationship=f"Deployed {when}. {CORRELATION_NOTE}",
            ))

    for n in notices.itertuples(index=False):
        start = _ts(n.reported_start_at)
        end = _ts(n.reported_end_at) if pd.notna(n.reported_end_at) else window.end
        if start < window.end and end > since:  # overlaps the period we look at
            hours = _hours(start, window.start)
            same = n.provider_code == window.segment
            when = f"{abs(hours):.1f} h before" if hours < 0 else f"{hours:.1f} h after"
            events.append(RelatedEvent(
                kind="provider_notice",
                event_id=n.incident_id,
                at=start,
                hours_from_anomaly_start=round(hours, 2),
                same_provider=same,
                description=f"{n.provider_code}: {n.title}",
                relationship=(
                    f"Notice posted {when} the anomaly began, by "
                    f"{'the same provider' if same else 'a different provider'}. {CORRELATION_NOTE}"
                ),
            ))

    return sorted(events, key=lambda e: e.at)


def change_around(
    hourly: pd.DataFrame, at: datetime, hours: int = 24, segment_col: str = "provider_code", significance: float = 0.01
) -> pd.DataFrame:
    """Failure rate per segment in the `hours` before vs after a moment (plus an 'ALL' row),
    with a two-proportion test so random wobble in small segments is not mistaken for a change.

    hourly: [segment_col, hour, attempts, failures]
    """
    at = _ts(at)
    h = hourly.assign(hour=pd.to_datetime(hourly["hour"], utc=True))
    before = h[(h["hour"] >= at - timedelta(hours=hours)) & (h["hour"] < at)]
    after = h[(h["hour"] >= at) & (h["hour"] < at + timedelta(hours=hours))]

    def totals(frame: pd.DataFrame) -> pd.DataFrame:
        t = frame.groupby(segment_col)[["attempts", "failures"]].sum()
        t.loc["ALL"] = t.sum()
        return t

    out = totals(before).join(totals(after), lsuffix="_before", rsuffix="_after", how="outer").fillna(0)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["rate_before"] = out["failures_before"] / out["attempts_before"]
        out["rate_after"] = out["failures_after"] / out["attempts_after"]
    out["change_pct_points"] = (out["rate_after"] - out["rate_before"]) * 100
    z, p = two_proportion_test(out["failures_after"], out["attempts_after"], out["failures_before"], out["attempts_before"])
    out["z_score"], out["p_value"] = z, p
    out["significant_increase"] = (out["p_value"] < significance) & (out["change_pct_points"] > 0)
    return out.reset_index().rename(columns={"index": segment_col})


def _ts(value) -> datetime:
    ts = pd.Timestamp(value)
    return (ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")).to_pydatetime()


def _hours(at: datetime, reference: datetime) -> float:
    return (at - reference).total_seconds() / 3600
