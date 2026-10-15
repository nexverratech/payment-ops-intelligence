"""Verification and Policy Agent.

Trust nothing that has not been checked, including the other agents and the LLM.

A. Recomputes key figures from the database and compares them with what the
   agents reported (overall rates, the anomaly window counts, the impact value).
B. Checks evidence integrity: every finding cites tool calls that really
   happened and succeeded.
C. Checks policy: no denied tool calls, every call within the agent's
   permissions, no LLM call over budget.
D. Checks every sentence the LLM wrote:
   - it must cite at least one known finding or hypothesis ID,
   - every number in it must appear in the cited material (copied, not invented),
   - it must not state a cause as proven fact ("caused", "root cause", "due to"...)
     unless it is clearly hedged ("may", "hypothesis", "not confirmed"...).
   Failing sentences are removed and listed with the reason.
E. Decides: the verified LLM report, or the template report if too much failed.
"""

from __future__ import annotations

import re

from payops.agents.schemas import NarrativeReport, NarrativeSection, RemovedSentence, Sentence, VerificationCheck, VerificationReport
from payops.agents.tools import TOOL_PERMISSIONS, AgentTools
from payops.analytics.models import AnalysisFilter

MAX_REMOVED_SHARE = 0.30   # more than 30% of sentences removed -> don't trust the LLM report

ID_PATTERN = re.compile(r"\b[A-Z]{1,6}(?:-[A-Z0-9]+)+\b")              # TX-1, DEP-0026, H-PROVIDER, TXN-00001
DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?Z?)?\b")
TIME_PATTERN = re.compile(r"\b\d{1,2}:\d{2}\b")
NUMBER_PATTERN = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
CAUSAL = re.compile(r"\b(caused|causes|causing|cause of|root cause|due to|because of|resulted in|results in|"
                    r"led to|leads to|responsible for|triggered|was the reason)\b", re.I)
HEDGE = re.compile(r"\b(may|might|could|possibl\w*|likely|unlikely|hypothes\w*|unconfirmed|not confirmed|not proven|"
                   r"correlat\w*|suggest\w*|consistent with|whether|not evidence|leading)\b", re.I)
PROOF = re.compile(r"\b(proves?|proven|confirms?|confirmed|definitely|certainly)\b", re.I)
NEGATION = re.compile(r"\b(not|no|never|un)\b|\bunconfirmed\b", re.I)


def run(state: dict, tools: AgentTools) -> dict:
    checks: list[VerificationCheck] = []
    checks += _recompute(state, tools)
    checks += _integrity_and_policy(state)

    narrative: NarrativeReport | None = state.get("narrative")
    result = state["result"]
    if narrative is None:
        return {"verification": VerificationReport(
            checks=checks, sentences_total=0, sentences_kept=0, removed=[], evidence_support_rate=None,
            unsupported_claim_rate=None, decision="template_report",
            reason=f"Template report used: {state.get('narrative_note', 'no LLM narrative')}."),
            "verified_narrative": None}

    sources = _sources(result)
    removed: list[RemovedSentence] = []

    def keep(sentences: list[Sentence]) -> list[Sentence]:
        kept = []
        for s in sentences:
            reasons = check_sentence(s, sources)
            if reasons:
                removed.append(RemovedSentence(text=s.text, reasons=reasons))
            else:
                kept.append(s)
        return kept

    verified = NarrativeReport(
        summary=keep(narrative.summary),
        sections=[NarrativeSection(title=sec.title, sentences=keep(sec.sentences)) for sec in narrative.sections],
        next_steps=keep(narrative.next_steps),
    )
    total = len(narrative.summary) + sum(len(s.sentences) for s in narrative.sections) + len(narrative.next_steps)
    kept = total - len(removed)
    figures_ok = all(c.status != "fail" for c in checks)
    share_removed = len(removed) / total if total else 1.0

    if total == 0 or not verified.summary:
        decision, reason = "template_report", "Template report used: the LLM report had no verifiable summary."
    elif share_removed > MAX_REMOVED_SHARE:
        decision, reason = "template_report", (f"Template report used: verification removed {len(removed)} of {total} "
                                               f"LLM sentences (limit {MAX_REMOVED_SHARE:.0%}).")
    elif not figures_ok:
        decision, reason = "template_report", "Template report used: a recomputation or policy check failed."
    else:
        decision, reason = "llm_report", "All checks passed."

    checks.append(VerificationCheck(name="llm_sentences", status="pass" if not removed else "fail",
                                    detail=f"{kept} of {total} sentences supported; {len(removed)} removed"))
    return {
        "verification": VerificationReport(
            checks=checks, sentences_total=total, sentences_kept=kept, removed=removed,
            evidence_support_rate=round(kept / total, 3) if total else None,
            unsupported_claim_rate=round(len(removed) / total, 3) if total else None,
            decision=decision, reason=reason),
        "verified_narrative": verified,
    }


# ---- A. recompute ------------------------------------------------------------------------------

def _recompute(state: dict, tools: AgentTools) -> list[VerificationCheck]:
    out = []
    plan, transaction, pattern, impact = (state.get(k) for k in ("plan", "transaction", "pattern", "impact"))
    if plan and transaction:
        current = AnalysisFilter(start=plan.current_start, end=plan.current_end, provider_codes=plan.provider_codes)
        for label, f, name in (("current", current, "failure_rate"),
                               ("previous", current.model_copy(update={"start": plan.baseline_start, "end": plan.baseline_end}),
                                "previous_failure_rate")):
            s = tools.summary(f)
            recomputed = round(100 * s["failures"] / s["attempts"], 3) if s["attempts"] else None
            reported = next(x.value for x in transaction.overall if x.name == name)
            out.append(VerificationCheck(
                name=f"recompute_{label}_failure_rate", status="pass" if recomputed == reported else "fail",
                detail=f"reported {reported}%, recomputed {recomputed}%"))
    if pattern and pattern.anomalies:
        a = pattern.anomalies[0]
        s = tools.summary(AnalysisFilter(start=a.start, end=a.end, provider_codes=(a.segment,)))
        same = s["attempts"] == a.attempts and s["failures"] == a.failures
        out.append(VerificationCheck(name="recompute_anomaly_window", status="pass" if same else "fail",
                                     detail=f"{a.segment}: reported {a.failures}/{a.attempts}, recomputed {s['failures']}/{s['attempts']}"))
        if impact and impact.basis == "anomaly_window":
            reported = next(x.value for x in impact.figures if x.name == "affected_transaction_value")
            ok = abs(reported - round(s["failed_value"], 2)) < 0.01
            out.append(VerificationCheck(name="recompute_affected_value", status="pass" if ok else "fail",
                                         detail=f"reported {reported:,.2f}, recomputed {s['failed_value']:,.2f}"))
    if not out:
        out.append(VerificationCheck(name="recompute", status="skipped", detail="nothing to recompute (earlier steps missing)"))
    return out


# ---- B and C. evidence integrity and policy -------------------------------------------------

def _integrity_and_policy(state: dict) -> list[VerificationCheck]:
    calls = state.get("tool_calls", [])
    ok_ids = {c.call_id for c in calls if c.status == "ok"}
    findings = state["result"]["findings"]
    broken = [f["finding_id"] for f in findings
              if not f["evidence"]["tool_call_ids"] or not set(f["evidence"]["tool_call_ids"]) <= ok_ids]
    denied = [c.call_id for c in calls if c.status == "denied"]
    outside = [c.call_id for c in calls if c.tool not in TOOL_PERMISSIONS.get(c.agent, frozenset())]
    over = [c for c in state.get("llm_calls", []) if c.status == "budget_exceeded"]
    return [
        VerificationCheck(name="evidence_integrity", status="fail" if broken else "pass",
                          detail=f"{len(findings) - len(broken)} of {len(findings)} findings cite successful tool calls"
                          + (f"; untraceable: {', '.join(broken)}" if broken else "")),
        VerificationCheck(name="tool_permissions", status="fail" if denied or outside else "pass",
                          detail=f"{len(calls)} tool calls; denied: {len(denied)}; outside permissions: {len(outside)}"),
        VerificationCheck(name="llm_call_budget", status="fail" if over else "pass",
                          detail=f"{len(state.get('llm_calls', []))} LLM calls recorded; over budget: {len(over)}"),
    ]


# ---- D. sentence checks -------------------------------------------------------------------------

def _sources(result: dict) -> dict[str, str]:
    """ID -> all text and numbers a sentence citing that ID may use."""
    out = {}
    for f in result["findings"]:
        out[f["finding_id"]] = " ".join([f["statement"]] + [str(x["value"]) for x in f["figures"]])
    for h in result["hypotheses"]:
        out[h["hypothesis_id"]] = " ".join([h["title"], f"rank {h['rank']} score {h['score']}"] + h["supporting"]
                                           + h["contradicting"] + h["next_checks"])
    return out


def extract_numbers(text: str) -> list[tuple[float, int]]:
    """Numbers in a text as (value, decimals), ignoring IDs, ISO dates and clock times."""
    text = TIME_PATTERN.sub(" ", DATE_PATTERN.sub(" ", ID_PATTERN.sub(" ", text)))
    out = []
    for m in NUMBER_PATTERN.finditer(text):
        raw = m.group().replace(",", "")
        out.append((float(raw), len(raw.split(".")[1]) if "." in raw else 0))
    return out


def _matches(value: float, decimals: int, allowed: list[float]) -> bool:
    for a in allowed:
        if value == a or round(a, decimals) == value or (a and abs(value - a) / abs(a) <= 0.005):
            return True
    return False


def check_sentence(s: Sentence, sources: dict[str, str]) -> list[str]:
    reasons = []
    if not s.cites:
        reasons.append("cites no finding")
    unknown = [c for c in s.cites if c not in sources]
    if unknown:
        reasons.append(f"cites unknown IDs: {', '.join(unknown)}")
    allowed = [v for c in s.cites if c in sources for v, _ in extract_numbers(sources[c])]
    unsupported = [v for v, d in extract_numbers(s.text)
                   if not (d == 0 and (v <= 10 or 1900 <= v <= 2100))   # small counts and years are not checked
                   and not _matches(v, d, allowed)]
    if unsupported:
        reasons.append("numbers not found in the cited findings: "
                       + ", ".join(str(int(v)) if v.is_integer() else str(v) for v in unsupported))
    if CAUSAL.search(s.text) and not HEDGE.search(s.text):
        reasons.append("states a cause as fact")
    if PROOF.search(s.text) and not NEGATION.search(s.text):
        reasons.append("claims something is proven or confirmed")
    return reasons
