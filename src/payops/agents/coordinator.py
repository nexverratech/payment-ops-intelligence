"""Operations Coordinator Agent.

Two jobs in the workflow:
* plan()     - at the start: decide WHAT to investigate (period, comparison, scope, steps).
* assemble() - at the end: collect all findings into one result, record what
               failed or was skipped, and list what remains uncertain.

Phase 5a: planning is deterministic (the last complete week vs the week before).
Phase 5b adds a local LLM to interpret the question, checked against these same rules.
"""

from __future__ import annotations

from datetime import timedelta

from payops.agents.schemas import Plan
from payops.agents.tools import AgentTools
from payops.analytics.metrics import last_complete_week

STEPS = ["transaction", "pattern", "forecast", "impact"]


def plan(state: dict, tools: AgentTools) -> dict:
    dataset = tools.dataset_window()
    as_of = state.get("as_of") or dataset["window_end"]
    as_of = min(as_of, dataset["window_end"])
    start, end = last_complete_week(as_of)
    if start - timedelta(days=7) < dataset["window_start"]:
        raise ValueError("Not enough data before the requested week to compare it with a previous week.")
    return {
        "dataset": dataset,
        "plan": Plan(
            question=state["question"], as_of=as_of,
            current_start=start, current_end=end,
            baseline_start=start - timedelta(days=7), baseline_end=start,
            steps=list(STEPS),
            interpretation=("Default weekly review: the last complete week (Monday-Sunday, UTC) compared with the "
                            "week before, across all providers. (Deterministic planner; the question text is not "
                            "interpreted until Phase 5b.)"),
        ),
    }


def assemble(state: dict, tools: AgentTools) -> dict:
    trace, errors = state.get("trace", []), state.get("errors", [])
    findings = []
    for key in ("transaction", "pattern", "forecast", "impact"):
        part = state.get(key)
        if part is not None:
            findings.extend(f.model_dump(mode="json") for f in part.findings)

    failed = sorted({t.agent for t in trace if t.status == "failed"})
    skipped = sorted({t.agent for t in trace if t.status == "skipped"})
    uncertainties = [
        "Causes are not proven: hypotheses are ranked by transparent evidence rules and need confirmation "
        "(e.g. the provider's own incident report).",
        "Forecasts assume the near future behaves like the recent past; they cannot predict new incidents.",
        "Handling-cost figures are hypothetical and depend on the stated assumptions.",
    ]
    uncertainties += [f"The {a} step failed, so its part of the analysis is missing." for a in failed]
    uncertainties += [f"The {a} step was skipped because an earlier step it depends on did not finish." for a in skipped]

    pattern = state.get("pattern")
    transaction = state.get("transaction")
    return {"result": {
        "status": "complete" if not failed and not skipped else "partial",
        "headline": _headline(transaction, pattern),
        "plan": state["plan"].model_dump(mode="json") if state.get("plan") else None,
        "findings": findings,
        "hypotheses": [h.model_dump(mode="json") for h in pattern.hypotheses] if pattern else [],
        "anomalies": [a.model_dump(mode="json") for a in pattern.anomalies] if pattern else [],
        "impact_basis": state["impact"].basis if state.get("impact") else None,
        "forecast": state["forecast"].model_dump(mode="json", exclude={"findings"}) if state.get("forecast") else None,
        "uncertainties": uncertainties,
        "failed_steps": failed,
        "skipped_steps": skipped,
        "errors": [e.model_dump() for e in errors],
    }}


def _headline(transaction, pattern) -> str:
    if transaction is None:
        return "The transaction analysis did not complete; see failed steps."
    rate = next(f.value for f in transaction.overall if f.name == "failure_rate")
    prev = next(f.value for f in transaction.overall if f.name == "previous_failure_rate")
    text = f"Failure rate {prev:.2f}% → {rate:.2f}%."
    if transaction.top_provider:
        text += f" {transaction.top_provider} explains {transaction.top_provider_share:.0%} of the extra failures."
    if pattern and pattern.hypotheses:
        text += f" Leading hypothesis (unconfirmed): {pattern.hypotheses[0].title}."
    return text
