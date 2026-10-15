"""Forecasting Agent.

Question it answers: "What should we expect next, and how much can we trust that?"
Uses the Phase 3 forecast (trend + weekday regression for volume, recent median
for failure rate), always reports the backtest against a naive baseline, and
refuses to forecast when there is too little history.
"""

from __future__ import annotations

from payops.agents.schemas import Evidence, Finding, ForecastFindings
from payops.agents.tools import AgentTools
from payops.analytics.forecast import forecast_daily
from payops.analytics.models import AnalysisFilter, Figure


def run(state: dict, tools: AgentTools) -> dict:
    plan, dataset = state["plan"], state["dataset"]
    end = min(plan.as_of, dataset["window_end"])
    n = len(tools.calls)
    daily = tools.time_series(AnalysisFilter(start=dataset["window_start"], end=end, provider_codes=plan.provider_codes),
                              "day", by_provider=False)
    ids = tools.ids_since(n)
    volume, rate = forecast_daily(daily.rename(columns={"day": "date"}))

    findings: list[Finding] = []
    if volume.status != "ok":
        findings.append(Finding(finding_id="FC-1", agent="forecast", kind="forecast",
                                statement="No forecast: " + " ".join(volume.limitations), evidence=Evidence(tool_call_ids=ids)))
        return {"forecast": ForecastFindings(volume=volume.model_dump(mode="json"), failure_rate=rate.model_dump(mode="json"),
                                             findings=findings)}

    first = volume.points[0]
    bt = volume.backtest
    trust = ("No backtest was possible (too little history)." if not bt else
             f"On the last {bt['holdout_days']:.0f} days the model's average error was {bt['model_mae']:,.0f} per day, "
             f"against {bt['seasonal_naive_mae']:,.0f} for the naive 'same day last week' guess, so it "
             f"{'beats' if volume.model_beats_baseline else 'does NOT beat'} the baseline.")
    findings.append(Finding(
        finding_id="FC-1", agent="forecast", kind="forecast",
        figures=[Figure(name="forecast_attempts_next_day", value=first.value, unit="payments", kind="estimate", method=volume.method)],
        statement=(f"About {first.value:,.0f} payment attempts are expected on {first.date} "
                   f"(range {first.lower:,.0f}–{first.upper:,.0f}). {trust}"),
        evidence=Evidence(tool_call_ids=ids, notes=volume.limitations),
    ))
    r = rate.points[0]
    findings.append(Finding(
        finding_id="FC-2", agent="forecast", kind="forecast",
        figures=[Figure(name="normal_failure_rate", value=round(r.value * 100, 3), unit="%", kind="estimate", method=rate.method)],
        statement=(f"A normal daily failure rate is about {r.value:.2%} (recent 10th–90th percentile {r.lower:.2%}–{r.upper:.2%}). "
                   f"A day above {r.upper:.2%} would be unusual. This cannot predict a new incident."),
        evidence=Evidence(tool_call_ids=ids, notes=rate.limitations),
    ))
    return {"forecast": ForecastFindings(volume=volume.model_dump(mode="json"), failure_rate=rate.model_dump(mode="json"),
                                         findings=findings)}
