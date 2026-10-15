"""Business Impact Agent.

Question it answers: "How big is this, in payments and money, and what if it continues?"

Basis, in order of preference:
1. The main anomaly window from the Pattern agent (most precise).
2. If no window was found (or the Pattern agent failed): the whole-period
   comparison from the Transaction agent.

Measured, estimated and hypothetical figures stay separate (see analytics/impact.py).
Transaction value is never presented as revenue or loss.
"""

from __future__ import annotations

from payops.agents.schemas import Evidence, Finding, ImpactFindings
from payops.agents.tools import AgentTools
from payops.analytics.impact import ImpactAssumptions, window_impact
from payops.analytics.models import AnalysisFilter


def run(state: dict, tools: AgentTools) -> dict:
    plan = state["plan"]
    assumptions = ImpactAssumptions()
    pattern, transaction = state.get("pattern"), state.get("transaction")

    n = len(tools.calls)
    if pattern is not None and pattern.anomalies:
        a = pattern.anomalies[0]
        details = tools.summary(AnalysisFilter(start=a.start, end=a.end, provider_codes=(a.segment,)))
        basis, scope = "anomaly_window", f"at {a.segment} between {a.start:%d %b %H:%M} and {a.end:%d %b %H:%M} UTC"
        attempts, failures, expected = a.attempts, a.failures, a.expected_failures
        hours = (a.end - a.start).total_seconds() / 3600
    elif transaction is not None:
        current = AnalysisFilter(start=plan.current_start, end=plan.current_end, provider_codes=plan.provider_codes)
        details = tools.summary(current)
        prev_rate = next(f.value for f in transaction.overall if f.name == "previous_failure_rate") / 100
        basis, scope = "period_comparison", "in the whole selected period (no anomaly window available)"
        attempts, failures = details["attempts"], details["failures"]
        expected = attempts * prev_rate
        hours = (plan.current_end - plan.current_start).total_seconds() / 3600
    else:
        raise ValueError("Neither anomaly windows nor a period comparison are available to size the impact.")
    ids = tools.ids_since(n)

    figures = window_impact(attempts=attempts, failures=failures, failed_value=details["failed_value"],
                            expected_failures=expected, duration_hours=hours, assumptions=assumptions)
    f = {x.name: x for x in figures}
    ev = Evidence(tool_call_ids=ids)
    findings = [
        Finding(finding_id="IM-1", agent="impact", kind="impact", evidence=ev,
                figures=[f["failed_payments"], f["affected_transaction_value"]],
                statement=(f"[measured] {f['failed_payments'].value:,.0f} payments failed {scope}, with a total transaction "
                           f"value of {f['affected_transaction_value'].value:,.2f} {assumptions.currency}. "
                           "This is the value of the payments, not revenue or loss: many customers retry.")),
        Finding(finding_id="IM-2", agent="impact", kind="impact", evidence=ev,
                figures=[f["expected_failures"], f["excess_failures"], f["excess_affected_value"]],
                statement=(f"[estimate] About {max(f['excess_failures'].value, 0):,.0f} of them are above the normal rate "
                           f"(expected {f['expected_failures'].value:,.0f}), worth about "
                           f"{f['excess_affected_value'].value:,.2f} {assumptions.currency} in transaction value.")),
        Finding(finding_id="IM-3", agent="impact", kind="impact", evidence=ev,
                figures=[f["projected_additional_failures"], f["handling_cost_low"], f["handling_cost_base"], f["handling_cost_high"]],
                statement=(f"[hypothetical] If the excess rate continued for {assumptions.projection_hours:g} more hours, about "
                           f"{f['projected_additional_failures'].value:,.0f} more payments would fail. At an assumed handling cost "
                           f"of {', '.join(f'{v:.2f}' for v in sorted(assumptions.cost_per_failure_scenarios.values()))} "
                           f"{assumptions.currency} per failure, the extra failures so far cost "
                           f"{f['handling_cost_low'].value:,.2f} – {f['handling_cost_high'].value:,.2f} {assumptions.currency}.")),
    ]
    return {"impact": ImpactFindings(basis=basis, figures=figures, assumptions=assumptions.model_dump(), findings=findings)}
