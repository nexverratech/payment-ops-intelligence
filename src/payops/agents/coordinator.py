"""Operations Coordinator Agent.

Four jobs in the workflow:
* plan()     - start: turn the question into a Plan. With the local LLM, the model
               PROPOSES (week, providers) as JSON and code VALIDATES the proposal
               against the loaded data. Anything invalid falls back to the default
               weekly review, and the plan says so.
* assemble() - after the analysis agents: collect findings, failed/skipped steps,
               uncertainties.
* narrate()  - ask the LLM to write the report from the findings (words only;
               numbers are copied from findings and checked later).
* finalize() - choose what the user sees: the verified LLM report, or the template
               report with the reason it was used.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from payops.agents.schemas import NarrativeReport, Plan, PlanProposal
from payops.agents.template_report import render as render_template
from payops.agents.tools import AgentTools
from payops.analytics.metrics import last_complete_week
from payops.llm.client import LLMBudgetExceeded, LLMClient, LLMOutputError, LLMUnavailable
from payops.llm.prompts import NARRATOR_SYSTEM, PLANNER_SYSTEM, narrator_user, planner_user, scope_text

STEPS = ["transaction", "pattern", "forecast", "impact"]
LLM_ERRORS = (LLMUnavailable, LLMOutputError, LLMBudgetExceeded)
DEFAULT_INTERPRETATION = ("Default weekly review: the last complete week (Monday-Sunday, UTC) compared with the "
                          "week before, across all providers.")


class PlanRejected(ValueError):
    """The LLM's proposal did not pass validation."""


# ---- plan --------------------------------------------------------------------------------

def plan(state: dict, tools: AgentTools, llm: LLMClient | None = None) -> dict:
    dataset = tools.dataset_window()
    as_of = min(state.get("as_of") or dataset["window_end"], dataset["window_end"])
    start, _ = last_complete_week(as_of)
    providers: tuple[str, ...] = ()
    planner, interpretation = "deterministic", DEFAULT_INTERPRETATION
    n = len(llm.calls) if llm else 0

    if llm is None:
        interpretation += " (LLM switched off: the question text was not interpreted.)"
    else:
        known = tools.providers()
        try:
            proposal = llm.generate("plan", PLANNER_SYSTEM, planner_user(
                state["question"], as_of.date().isoformat(), dataset["window_start"].date().isoformat(),
                dataset["window_end"].date().isoformat(), known.to_dict("records")), PlanProposal)
            start, providers = validate_proposal(proposal, set(known["provider_code"]), dataset, as_of)
            planner = "llm"
            scope = ", ".join(providers) if providers else "all providers"
            interpretation = (f"Read by the local LLM and checked by code: the week starting {start:%d %b %Y} "
                              f"compared with the week before; scope: {scope}. Model's reading: "
                              f"\"{proposal.reasoning.strip()[:200]}\"")
        except PlanRejected as exc:
            interpretation += f" (The LLM's plan was rejected by validation: {exc}.)"
        except LLM_ERRORS as exc:
            interpretation += f" (Local LLM not used for planning: {exc})"

    if start - timedelta(days=7) < dataset["window_start"]:
        raise ValueError("Not enough data before the requested week to compare it with a previous week.")
    update = {
        "dataset": dataset,
        "plan": Plan(question=state["question"], as_of=as_of, current_start=start, current_end=start + timedelta(days=7),
                     baseline_start=start - timedelta(days=7), baseline_end=start, provider_codes=providers,
                     steps=list(STEPS), interpretation=interpretation, planner=planner),
    }
    if llm:
        update["llm_calls"] = llm.calls[n:]
    return update


def validate_proposal(p: PlanProposal, known_providers: set[str], dataset: dict, as_of: datetime) -> tuple[datetime, tuple[str, ...]]:
    """Accept the LLM's proposal only if it makes sense for the loaded data."""
    codes = tuple(sorted({c.strip().upper() for c in p.provider_codes}))
    unknown = [c for c in codes if c not in known_providers]
    if unknown:
        raise PlanRejected(f"unknown provider code(s) {', '.join(unknown)}")
    if set(codes) == known_providers:
        codes = ()          # naming every provider is the same as "all providers"
    if not p.week_containing:
        start, _ = last_complete_week(as_of)
        return start, codes
    try:
        day = date.fromisoformat(p.week_containing.strip()[:10])
    except ValueError:
        raise PlanRejected(f"'{p.week_containing}' is not a date") from None
    start = datetime.combine(day - timedelta(days=day.weekday()), time.min, tzinfo=timezone.utc)
    if start + timedelta(days=7) > as_of:
        raise PlanRejected(f"the week starting {start:%d %b %Y} is not complete in the data")
    if start - timedelta(days=7) < dataset["window_start"]:
        raise PlanRejected(f"the week starting {start:%d %b %Y} has no previous week in the data to compare with")
    return start, codes


# ---- assemble -------------------------------------------------------------------------------

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
    reasons = {t.agent: t.message for t in trace if t.status == "skipped"}
    uncertainties += [f"The {a} step was skipped ({reasons.get(a) or 'an earlier step it depends on did not finish'})."
                      for a in skipped]

    pattern, transaction = state.get("pattern"), state.get("transaction")
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


# ---- narrate --------------------------------------------------------------------------------

LLM_FAILURE_NOTE = "local LLM unavailable for the report"   # a resumed run tries the LLM again


def narrate(state: dict, tools: AgentTools, llm: LLMClient | None = None) -> dict:
    result = state["result"]
    if llm is None:
        return {"narrative": None, "narrative_note": "LLM switched off"}
    if not result["findings"]:
        return {"narrative": None, "narrative_note": "no findings to write about"}
    n = len(llm.calls)
    try:
        plan = state.get("plan")
        scope = scope_text(plan.current_start, plan.current_end, plan.baseline_start, plan.baseline_end) if plan else None
        report = llm.generate("report", NARRATOR_SYSTEM,
                              narrator_user(state["question"], result["findings"], result["hypotheses"], result["uncertainties"],
                                            scope),
                              NarrativeReport)
        note = "written by the local LLM"
    except LLM_ERRORS as exc:
        report, note = None, f"{LLM_FAILURE_NOTE}: {exc}"
    return {"narrative": report, "narrative_note": note, "llm_calls": llm.calls[n:]}


# ---- finalize -------------------------------------------------------------------------------

def finalize(state: dict, tools: AgentTools) -> dict:
    result = dict(state["result"])
    # assemble ran before narrate and verify; count those steps too, so a report step that
    # failed or ran out of time makes the investigation "partial" (and therefore resumable).
    trace = state.get("trace", [])
    failed = sorted({t.agent for t in trace if t.status == "failed"})
    skipped = sorted({t.agent for t in trace if t.status == "skipped"})
    for a in failed:
        if a not in result["failed_steps"]:
            result["uncertainties"] = result["uncertainties"] + [f"The {a} step failed, so its part of the analysis is missing."]
    for t in trace:
        if t.status == "skipped" and t.agent not in result["skipped_steps"]:
            result["uncertainties"] = result["uncertainties"] + [f"The {t.agent} step was skipped ({t.message})."]
    result.update(failed_steps=failed, skipped_steps=skipped,
                  status="complete" if not failed and not skipped else "partial",
                  errors=[e.model_dump() for e in state.get("errors", [])])
    verification = state.get("verification")
    verified = state.get("verified_narrative")
    if verification is not None and verification.decision == "llm_report" and verified is not None:
        mode, reason = "llm", f"LLM report; {verification.sentences_kept} of {verification.sentences_total} sentences passed verification."
    else:
        mode = "template"
        reason = (verification.reason if verification is not None and state.get("narrative") is not None
                  else f"Template report used: {state.get('narrative_note', 'no narrative')}.")
    result.update({
        "mode": mode,
        "mode_reason": reason,
        "narrative": verified.model_dump() if verified is not None and mode == "llm" else None,
        "verification": verification.model_dump() if verification is not None else None,
    })
    result["report_text"] = render_llm_report(result) if mode == "llm" else (
        f"[{reason}]\n\n" + render_template(result) + _rejected_llm_report(result["verification"]))
    return {"result": result}


def _rejected_llm_report(v: dict | None) -> str:
    """When an LLM report was checked but not used, show why, so nobody has to dig for it."""
    if not v or not v["sentences_total"]:
        return ""
    out = ["", "", "WHY THE LLM REPORT WAS NOT USED",
           f"  {v['sentences_kept']} of {v['sentences_total']} sentences passed verification"
           + (f"; {v['duplicates_dropped']} exact repeats dropped" if v.get("duplicates_dropped") else "") + "."]
    out += [f"  removed: \"{r['text']}\" [{', '.join(r.get('cites', []))}] ({'; '.join(r['reasons'])})" for r in v["removed"]]
    out += [f"  [{c['status'].upper()}] {c['name']}: {c['detail']}" for c in v["checks"] if c["status"] == "fail"]
    return "\n".join(out)


def render_llm_report(result: dict) -> str:
    v, n = result["verification"], result["narrative"]
    plan = result.get("plan") or {}
    out = ["INVESTIGATION REPORT",
           "Written by the local LLM from findings computed by code; every sentence was checked by the "
           f"Verification agent ({v['sentences_kept']} of {v['sentences_total']} kept). Numbers come from code, not the model.",
           ""]
    if plan:
        out += [f"Question: {plan['question']}", f"Scope:    {plan['interpretation']}", ""]
    out += ["SUMMARY"] + [f"  {s['text']} [{', '.join(s['cites'])}]" for s in n["summary"]]
    for sec in n["sections"]:
        if sec["sentences"]:
            out += ["", sec["title"].upper()] + [f"  {s['text']} [{', '.join(s['cites'])}]" for s in sec["sentences"]]
    if n["next_steps"]:
        out += ["", "NEXT STEPS"] + [f"  - {s['text']} [{', '.join(s['cites'])}]" for s in n["next_steps"]]
    out += ["", "WHAT REMAINS UNCERTAIN"] + [f"  - {u}" for u in result["uncertainties"]]
    out += ["", "VERIFICATION"] + [f"  [{c['status'].upper()}] {c['name']}: {c['detail']}" for c in v["checks"]]
    for r in v["removed"]:
        out.append(f"  removed: \"{r['text']}\" [{', '.join(r.get('cites', []))}] ({'; '.join(r['reasons'])})")
    return "\n".join(out)
