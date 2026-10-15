"""Deterministic weekly investigation: every analytics step, no LLM.

Run:  python -m payops.analytics.report
      python -m payops.analytics.report --as-of 2026-09-29T00:00:00Z --json

This does what an analyst would do by hand: compare last week with the week
before, find which provider explains the change, look for anomaly windows,
list nearby deployments and provider notices, size the impact, and forecast
the next week. Phase 5 agents call the same building blocks; this report is
also the clearly labelled fallback when the local LLM is unavailable.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy.engine import Connection

from payops.analytics import queries
from payops.analytics.detection import detect_anomalies
from payops.analytics.events import change_around, related_events
from payops.analytics.forecast import forecast_daily
from payops.analytics.impact import ImpactAssumptions, window_impact
from payops.analytics.metrics import compare_periods, last_complete_week, two_proportion_test
from payops.analytics.models import AnalysisFilter, Figure
from payops.analytics.serialize import to_records as _records

DETECTION_HISTORY = timedelta(days=9)  # 7-day baseline + 24 h gap + margin


def build_report(conn: Connection, as_of: datetime | None = None, assumptions: ImpactAssumptions | None = None) -> dict:
    window = queries.dataset_window(conn)
    as_of = as_of or window["window_end"]
    cur_start, cur_end = last_complete_week(as_of)
    if cur_start < window["window_start"] + timedelta(days=7):
        raise ValueError("Not enough data before the requested week to compare with a previous week.")
    current = AnalysisFilter(start=cur_start, end=cur_end)
    baseline = current.shifted(timedelta(days=-7))

    # 1. Overall change ---------------------------------------------------------
    cur_sum, base_sum = queries.summary(conn, current), queries.summary(conn, baseline)
    z, p = two_proportion_test([cur_sum["failures"]], [cur_sum["attempts"]], [base_sum["failures"]], [base_sum["attempts"]])
    cur_rate = cur_sum["failures"] / cur_sum["attempts"]
    base_rate = base_sum["failures"] / base_sum["attempts"]
    overall = [
        Figure(name="attempts", value=cur_sum["attempts"], unit="payments", kind="measured", method="Count of payment attempts in the period."),
        Figure(name="failures", value=cur_sum["failures"], unit="payments", kind="measured", method="Count of FAILED payments in the period."),
        Figure(name="failure_rate", value=round(cur_rate * 100, 3), unit="%", kind="measured", method="Failures / attempts."),
        Figure(name="previous_failure_rate", value=round(base_rate * 100, 3), unit="%", kind="measured", method="Same, for the previous week."),
        Figure(name="change", value=round((cur_rate - base_rate) * 100, 3), unit="percentage points", kind="measured", method="Difference of the two rates."),
        Figure(name="z_score", value=round(float(z[0]), 2), unit="standard deviations", kind="estimate",
               method="Two-proportion z-test of the two weekly rates; says whether the change exceeds random variation, not why."),
    ]

    # 2. Which provider explains the change -------------------------------------
    by_provider = compare_periods(queries.breakdown(conn, current, "provider"),
                                  queries.breakdown(conn, baseline, "provider"), key="provider_code")
    top = by_provider[(by_provider["excess_failures"] > 0) & by_provider["significant"]].head(1)
    top_contributor = None
    if len(top):
        code = top.iloc[0]["provider_code"]
        cur_p, base_p = current.model_copy(update={"provider_codes": (code,)}), baseline.model_copy(update={"provider_codes": (code,)})
        reasons_cur = queries.failure_reasons(conn, cur_p).rename(columns={"failures": "failures_current"})
        reasons_base = queries.failure_reasons(conn, base_p).rename(columns={"failures": "failures_baseline"})
        reasons = reasons_cur[["failure_reason", "failures_current"]].merge(
            reasons_base[["failure_reason", "failures_baseline"]], on="failure_reason", how="outer").fillna(0)
        reasons["change"] = reasons["failures_current"] - reasons["failures_baseline"]
        top_contributor = {
            "provider_code": code,
            "contribution_share": float(top.iloc[0]["contribution_share"]),
            "reasons": _records(reasons.sort_values("change", ascending=False)),
            "evidence_transaction_ids": queries.sample_failed_transactions(conn, cur_p, limit=5),
        }

    # 3. Anomaly windows that touch the week ------------------------------------
    history = AnalysisFilter(start=max(cur_start - DETECTION_HISTORY, window["window_start"]), end=cur_end)
    hourly = queries.time_series(conn, history, "hour", by_provider=True)
    deployments, notices = queries.events(conn, history.start, cur_end)
    anomalies = []
    for w in detect_anomalies(hourly):
        if not (w.start < cur_end and w.end > cur_start):
            continue
        in_window = AnalysisFilter(start=w.start, end=w.end, provider_codes=(w.segment,))
        details = queries.summary(conn, in_window)
        events = related_events(w, deployments, notices)
        anomalies.append({
            "window": w.model_dump(mode="json"),
            "related_events": [e.model_dump(mode="json") for e in events],
            "deployment_checks": [
                {"deployment_id": e.event_id, "change_by_provider_24h": _records(change_around(hourly, e.at, 24))}
                for e in events if e.kind == "deployment"
            ],
            "impact": [f.model_dump() for f in window_impact(
                attempts=w.attempts, failures=w.failures, failed_value=details["failed_value"],
                expected_failures=w.expected_failures,
                duration_hours=(w.end - w.start).total_seconds() / 3600, assumptions=assumptions)],
            "evidence_transaction_ids": queries.sample_failed_transactions(conn, in_window, limit=5),
        })

    # 4. Forecast -----------------------------------------------------------------
    daily = queries.time_series(conn, AnalysisFilter(start=window["window_start"], end=min(as_of, window["window_end"])), "day", by_provider=False)
    volume, rate = forecast_daily(daily.rename(columns={"day": "date"}))

    return {
        "dataset_id": window["dataset_id"],
        "as_of": _iso(as_of),
        "period": {"current": [_iso(cur_start), _iso(cur_end)], "baseline": [_iso(baseline.start), _iso(baseline.end)]},
        "overall": [f.model_dump() for f in overall],
        "by_provider": _records(by_provider),
        "top_contributor": top_contributor,
        "anomalies": anomalies,
        "forecast": {"attempts": volume.model_dump(), "failure_rate": rate.model_dump()},
        "generated_by": "deterministic analytics (no LLM)",
    }


def render_text(r: dict) -> str:
    out = [
        "PAYMENT FAILURE REVIEW (deterministic analytics, no LLM)",
        f"Dataset {r['dataset_id']}  |  week {r['period']['current'][0][:10]} to {r['period']['current'][1][:10]} (end exclusive)",
        "",
        "1. Overall",
    ]
    out += [f"   [{f['kind']}] {f['name']}: {_num(f['value'])} {f['unit']}" for f in r["overall"]]

    out += ["", "2. Change by provider (largest excess failures first)"]
    for row in r["by_provider"]:
        out.append(
            f"   {row['provider_code']:<10} {_pct(row['rate_baseline'])} -> {_pct(row['rate_current'])}  "
            f"excess {row['excess_failures']:7.1f}  share {row['contribution_share']:5.1%}  "
            f"{'significant' if row['significant'] else 'not significant'}"
        )
    tc = r["top_contributor"]
    if tc:
        out += ["", f"3. Top contributor: {tc['provider_code']} ({tc['contribution_share']:.0%} of the extra failures)"]
        out += [f"   {x['failure_reason']:<22} {int(x['failures_baseline']):5d} -> {int(x['failures_current']):5d}" for x in tc["reasons"][:4]]
        out.append(f"   Evidence (first failed payments): {', '.join(tc['evidence_transaction_ids'])}")
    else:
        out += ["", "3. No provider shows a significant increase."]

    out += ["", f"4. Anomaly windows touching the week: {len(r['anomalies'])}"]
    for a in r["anomalies"]:
        w = a["window"]
        out.append(f"   {w['segment']}: {w['start'][:16]} -> {w['end'][:16]} UTC, failure rate {w['baseline_rate']:.2%} -> {w['window_rate']:.2%} "
                   f"(peak hour {w['peak_hour_rate']:.1%}, max z {w['max_z']})")
        for e in a["related_events"]:
            out.append(f"     - {e['kind']} {e['event_id']} ({e['description']}): {e['relationship']}")
        for check in a["deployment_checks"]:
            moved = [c["provider_code"] for c in check["change_by_provider_24h"]
                     if c["provider_code"] != "ALL" and c["significant_increase"]]
            out.append(f"     - 24 h after {check['deployment_id']}, providers with a statistically significant rise: "
                       f"{', '.join(moved) or 'none'} (of {len(check['change_by_provider_24h']) - 1})")
        for f in a["impact"]:
            out.append(f"     [{f['kind']}] {f['name']}: {_num(f['value'])} {f['unit']}")

    fv, fr = r["forecast"]["attempts"], r["forecast"]["failure_rate"]
    out += ["", "5. Forecast (next 7 days)"]
    if fv["status"] == "ok":
        out.append(f"   [estimate] attempts/day: {fv['points'][0]['value']:,.0f} (range {fv['points'][0]['lower']:,.0f}-{fv['points'][0]['upper']:,.0f}) on {fv['points'][0]['date']}")
        b = fv["backtest"]
        if b:
            out.append(f"   backtest on {b['holdout_days']:.0f} held-out days: model error {b['model_mae']:,.0f}/day vs "
                       f"naive 'same day last week' {b['seasonal_naive_mae']:,.0f}/day -> model "
                       f"{'beats' if fv['model_beats_baseline'] else 'does NOT beat'} the baseline")
        out.append(f"   [estimate] normal failure rate: {fr['points'][0]['value']:.2%} (band {fr['points'][0]['lower']:.2%}-{fr['points'][0]['upper']:.2%})")
    else:
        out.append("   " + "; ".join(fv["limitations"]))
    out += ["", "Timing relationships are correlations, not proven causes. Hypothetical figures depend on stated assumptions."]
    return "\n".join(out)


def _num(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _pct(value: float | None) -> str:
    return "  n/a " if value is None else f"{value * 100:5.2f}%"


def _iso(dt: datetime) -> str:
    return pd.Timestamp(dt).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic weekly payment failure review.")
    parser.add_argument("--as-of", help="UTC time, e.g. 2026-09-29T00:00:00Z (default: end of the loaded data)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)

    from payops.config import get_settings
    from payops.db.connection import get_readonly_engine

    as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00")).astimezone(timezone.utc) if args.as_of else None
    with get_readonly_engine(get_settings()).connect() as conn:
        report = build_report(conn, as_of)
    print(json.dumps(report, indent=2) if args.json else render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
