"""Pattern Investigation Agent.

Question it answers: "When did it start and stop, what happened around it,
and which explanations does the evidence support?"

1. Anomaly windows (Phase 3 detection) that overlap the period, with the
   dominant failure reason inside each window.
2. Deployments and provider notices near the main window, and for each
   deployment: which providers got significantly worse in the 24 h after it.
3. Ranked hypotheses using transparent evidence points (rules below).
   Hypotheses are ranked, never "proven": the output says so.

Scoring rules (all visible in the output as supporting/contradicting lines):
  Provider-side problem at provider P
    +2  only P shows an anomaly in the period          (-1 if others do too)
    +1  most extra failures are a technical reason (TIMEOUT, PROVIDER_ERROR)
    +2  P published a status notice overlapping the anomaly
    +1  after a nearby deployment, only P got significantly worse
  Our deployment D caused it
    +1  D happened within 24 h before the anomaly began
    +2  two or more providers got significantly worse after D (shared cause)
    -1  only one provider got worse after D (our software is shared by all providers)
  Customer-side change (only if the dominant reason is a customer reason)
    +2  most extra failures are customer declines (e.g. INSUFFICIENT_FUNDS)
"""

from __future__ import annotations

from datetime import timedelta

from payops.agents.schemas import AnomalySummary, Evidence, Finding, Hypothesis, PatternFindings
from payops.agents.tools import AgentTools
from payops.analytics.detection import detect_anomalies
from payops.analytics.events import change_around, related_events
from payops.analytics.models import AnalysisFilter, Figure

DETECTION_HISTORY = timedelta(days=9)   # 7-day baseline + 24 h gap + margin
TECHNICAL_REASONS = {"TIMEOUT", "PROVIDER_ERROR"}
CUSTOMER_REASONS = {"INSUFFICIENT_FUNDS", "DO_NOT_HONOR", "AUTHENTICATION_FAILED", "INVALID_DETAILS", "FRAUD_SUSPECTED"}


def run(state: dict, tools: AgentTools) -> dict:
    plan, dataset = state["plan"], state["dataset"]
    history = AnalysisFilter(start=max(plan.current_start - DETECTION_HISTORY, dataset["window_start"]),
                             end=plan.current_end, provider_codes=plan.provider_codes)

    n = len(tools.calls)
    hourly = tools.time_series(history, "hour", by_provider=True)
    detect_ids = tools.ids_since(n)
    windows = [w for w in detect_anomalies(hourly) if w.start < plan.current_end and w.end > plan.current_start] if len(hourly) else []

    anomalies: list[AnomalySummary] = []
    reason_ids: dict[str, list[str]] = {}
    sample_ids: dict[str, list[str]] = {}
    for w in windows:
        n = len(tools.calls)
        in_window = AnalysisFilter(start=w.start, end=w.end, provider_codes=(w.segment,))
        reasons = tools.failure_reasons(in_window)
        top_reason, share, samples = None, None, []
        if len(reasons):
            top_reason = str(reasons.iloc[0]["failure_reason"])
            share = float(reasons.iloc[0]["failures"]) / float(reasons["failures"].sum())
            samples = tools.sample_failed_transactions(in_window.model_copy(update={"failure_reasons": (top_reason,)}), 5)
        reason_ids[w.segment + w.start.isoformat()] = tools.ids_since(n)
        sample_ids[w.segment + w.start.isoformat()] = samples
        anomalies.append(AnomalySummary(
            segment=w.segment, start=w.start, end=w.end, attempts=w.attempts, failures=w.failures,
            expected_failures=w.expected_failures, excess_failures=w.excess_failures, baseline_rate=w.baseline_rate,
            window_rate=w.window_rate, max_z=w.max_z, dominant_reason=top_reason, dominant_reason_share=share,
        ))

    findings: list[Finding] = []
    if not anomalies:
        findings.append(Finding(
            finding_id="PT-1", agent="pattern", kind="observation",
            statement=("No anomaly window was detected in the period. Any change was gradual or too small for "
                       "the detection rules (z >= 4 for 2+ hours and >= 15 extra failures)."),
            evidence=Evidence(tool_call_ids=detect_ids),
        ))
        return {"pattern": PatternFindings(anomalies=[], related_events=[], hypotheses=[], findings=findings)}

    for i, (a, w) in enumerate(zip(anomalies, windows), start=1):
        findings.append(Finding(
            finding_id=f"PT-{i}", agent="pattern", kind="observation",
            figures=[
                Figure(name="window_failure_rate", value=round(a.window_rate * 100, 2), unit="%", kind="measured",
                       method="Failures / attempts inside the anomaly window."),
                Figure(name="baseline_failure_rate", value=round(a.baseline_rate * 100, 2), unit="%", kind="estimate",
                       method="Provider's rate over the 7 days ending 24 h before the window."),
                Figure(name="excess_failures", value=a.excess_failures, unit="payments", kind="estimate",
                       method="Failures minus attempts x baseline rate."),
            ],
            statement=(f"{a.segment}: failure rate {a.baseline_rate:.2%} → {a.window_rate:.2%} from "
                       f"{a.start:%d %b %H:%M} to {a.end:%d %b %H:%M} UTC ({a.excess_failures:,.0f} extra failures, "
                       f"peak z = {a.max_z})"
                       + (f"; {a.dominant_reason_share:.0%} of failures in the window were {a.dominant_reason}." if a.dominant_reason else ".")),
            evidence=Evidence(tool_call_ids=detect_ids + reason_ids[a.segment + a.start.isoformat()],
                              sample_transaction_ids=sample_ids[a.segment + a.start.isoformat()]),
        ))

    # Events around the main (largest) window ------------------------------------------
    main, main_window = anomalies[0], windows[0]
    n = len(tools.calls)
    deployments, notices = tools.events(history.start, plan.current_end)
    event_ids = tools.ids_since(n)
    events = related_events(main_window, deployments, notices)
    checks = {
        e.event_id: change_around(hourly, e.at, 24)
        for e in events if e.kind == "deployment" and e.hours_from_anomaly_start <= 0
    }
    if events:
        findings.append(Finding(
            finding_id=f"PT-{len(anomalies) + 1}", agent="pattern", kind="observation",
            statement="Around the main anomaly: " + " ".join(f"{e.event_id} ({e.description}): {e.relationship}" for e in events),
            evidence=Evidence(tool_call_ids=event_ids),
        ))

    hypotheses = rank_hypotheses(main, anomalies, events, checks, evidence_ids=detect_ids + event_ids)
    leading = hypotheses[0] if hypotheses else None
    if leading:
        findings.append(Finding(
            finding_id=f"PT-{len(anomalies) + 2}", agent="pattern", kind="hypothesis",
            statement=(f"Leading hypothesis (not confirmed): {leading.title} (evidence score {leading.score}). "
                       "Ranking uses transparent evidence rules; it shows what the data supports, not a proven cause."),
            evidence=leading.evidence,
        ))
    return {"pattern": PatternFindings(
        anomalies=anomalies, related_events=[e.model_dump(mode="json") for e in events],
        hypotheses=hypotheses, findings=findings,
    )}


def rank_hypotheses(main: AnomalySummary, anomalies: list[AnomalySummary], events: list, checks: dict,
                    evidence_ids: list[str]) -> list[Hypothesis]:
    """Score candidate explanations with the rules in the module docstring."""
    candidates: list[dict] = []
    p = main.segment

    # Provider-side ---------------------------------------------------------------------
    sup, con, score = [], [], 0
    others = [a for a in anomalies if a.segment != p and a.start < main.end and a.end > main.start]
    if others:
        score -= 1
        con.append(f"Other providers also show anomalies at the same time: {', '.join(a.segment for a in others)}.")
    else:
        score += 2
        sup.append(f"Only {p} shows an anomaly in this period.")
    if main.dominant_reason in TECHNICAL_REASONS:
        score += 1
        sup.append(f"{main.dominant_reason_share:.0%} of failures in the window are {main.dominant_reason}, a technical failure type.")
    elif main.dominant_reason:
        con.append(f"The dominant failure reason ({main.dominant_reason}) is not a technical one.")
    notices = [e for e in events if e.kind == "provider_notice" and e.same_provider]
    if notices:
        score += 2
        sup.append(f"{p} published notice {notices[0].event_id} ({notices[0].hours_from_anomaly_start:+.1f} h from the anomaly start).")
    only_p = [d for d, df in checks.items() if _risers(df) == [p]]
    if only_p:
        score += 1
        sup.append(f"After deployment {only_p[0]}, only {p} got significantly worse; our software is shared by all providers.")
    candidates.append(dict(hypothesis_id="H-PROVIDER", title=f"Provider-side problem at {p}", score=score, supporting=sup,
                           contradicting=con, next_checks=[
                               f"Ask {p} for their incident report and exact timeline.",
                               f"Compare {p} response times before and during the window.",
                               f"Check whether payments retried through another provider succeeded."]))

    # Each deployment before the anomaly --------------------------------------------------------
    for e in events:
        if e.kind != "deployment" or e.hours_from_anomaly_start > 0:
            continue
        sup, con, score = [f"Deployed {abs(e.hours_from_anomaly_start):.1f} h before the anomaly began."], [], 1
        risers = _risers(checks.get(e.event_id))
        if len(risers) >= 2:
            score += 2
            sup.append(f"{len(risers)} providers got significantly worse after it ({', '.join(risers)}), consistent with a shared cause.")
        elif len(risers) == 1:
            score -= 1
            con.append(f"Only {risers[0]} got significantly worse after it, although our software serves all providers.")
        else:
            con.append("No provider got significantly worse in the 24 h after it.")
        candidates.append(dict(hypothesis_id=f"H-{e.event_id}", title=f"Our deployment {e.event_id} ({e.description})",
                               score=score, supporting=sup, contradicting=con, next_checks=[
                                   f"Review the change in {e.event_id} and its error logs for {p} requests.",
                                   "If still in doubt, test the previous version against the provider's sandbox."]))

    # Customer-side (only if the dominant reason points at customers) -------------------------
    if main.dominant_reason in CUSTOMER_REASONS:
        candidates.append(dict(hypothesis_id="H-CUSTOMER", title="Customer-side change (e.g. card issuers declining more)",
                               score=2, supporting=[f"Most failures are {main.dominant_reason}, a customer decline."],
                               contradicting=[], next_checks=["Check whether one card issuer or region dominates the declines."]))

    candidates.sort(key=lambda c: (-c["score"], c["hypothesis_id"]))
    out = []
    for rank, c in enumerate(candidates, start=1):
        status = "unlikely" if c["score"] <= 0 else ("leading (unconfirmed)" if rank == 1 and c["score"] >= 2 else "alternative")
        out.append(Hypothesis(rank=rank, status=status, evidence=Evidence(tool_call_ids=evidence_ids), **c))
    return out


def _risers(df) -> list[str]:
    """Providers with a statistically significant rise in the 24 h after a moment."""
    if df is None or not len(df):
        return []
    rows = df[(df["provider_code"] != "ALL") & df["significant_increase"]]
    return sorted(str(x) for x in rows["provider_code"])
