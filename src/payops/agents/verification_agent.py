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
   - every written date ("21 September") must be a date of the investigation
     period or a date that appears in the findings,
   - it must not state a cause as proven fact ("caused", "root cause", "due to"...)
     unless it is clearly hedged ("may", "hypothesis", "not confirmed"...).
   Failing sentences are removed and listed with the reason. Exact repeats of an
   earlier sentence are dropped first (a quality issue, not a truth issue).
E. Decides: the verified LLM report, or the template report if too much failed.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

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
# Claims that something IS proven. The base forms "to confirm" / "to prove" are an
# intention to check (a next step), so they are not matched.
PROOF = re.compile(r"\b(proves|proved|proven|confirms|confirmed|definitely|certainly|undoubtedly)\b", re.I)
NOT_A_CLAIM = re.compile(r"\b(not|no|never|yet|until|unless|if|once|whether|should|would|could|can|must|needs?)\b"
                         r"|\bunconfirmed\b", re.I)

# "May" only with a capital M, so "3 may be" is not read as a date.
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|(?-i:May)|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
DAY_MONTH = re.compile(rf"\b{_DAY}\s+(?:of\s+)?{_MONTH}(?:,?\s+(\d{{4}}))?\b", re.I)   # 21 September 2026
MONTH_DAY = re.compile(rf"\b{_MONTH}\s+{_DAY}\b(?:,?\s+(\d{{4}}))?", re.I)              # September 21, 2026


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
    dates = known_dates(result, sources)
    removed: list[RemovedSentence] = []
    seen: set[str] = set()
    duplicates = 0

    def keep(sentences: list[Sentence]) -> list[Sentence]:
        nonlocal duplicates
        kept = []
        for s in sentences:
            key = " ".join(s.text.lower().split())
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            reasons = check_sentence(s, sources, dates)
            if reasons:
                removed.append(RemovedSentence(text=s.text, cites=s.cites, reasons=reasons))
            else:
                kept.append(s)
        return kept

    verified = NarrativeReport(
        summary=keep(narrative.summary),
        sections=[NarrativeSection(title=sec.title, sentences=keep(sec.sentences)) for sec in narrative.sections],
        next_steps=keep(narrative.next_steps),
    )
    total = (len(narrative.summary) + sum(len(s.sentences) for s in narrative.sections)
             + len(narrative.next_steps) - duplicates)          # repeats are not counted
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
                                    detail=f"{kept} of {total} sentences supported; {len(removed)} removed"
                                    + (f"; {duplicates} exact repeats dropped" if duplicates else "")))
    return {
        "verification": VerificationReport(
            checks=checks, sentences_total=total, sentences_kept=kept, removed=removed, duplicates_dropped=duplicates,
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
    for h in result["hypotheses"]:
        out[h["hypothesis_id"]] = " ".join([h["title"], f"rank {h['rank']} score {h['score']}"] + h["supporting"]
                                           + h["contradicting"] + h["next_checks"])
    leading = next((h["hypothesis_id"] for h in result["hypotheses"] if h["rank"] == 1), None)
    for f in result["findings"]:
        text = " ".join([f["statement"]] + [str(x["value"]) for x in f["figures"]])
        if f["kind"] == "hypothesis" and leading:
            # e.g. PT-3 "Leading hypothesis (not confirmed): ..." is a summary of the rank-1
            # hypothesis, so citing it covers that hypothesis's evidence too.
            text += " " + out[leading]
        out[f["finding_id"]] = text
    return out


def known_dates(result: dict, sources: dict[str, str]) -> set[tuple[int, int]]:
    """(month, day) pairs a sentence may mention: every day of the investigated and
    comparison weeks, plus any date written in the findings and hypotheses."""
    out: set[tuple[int, int]] = set()
    plan = result.get("plan") or {}
    if plan.get("baseline_start") and plan.get("current_end"):
        d = _as_date(plan["baseline_start"])
        end = _as_date(plan["current_end"])
        while d <= end:                       # the end date itself is often written ("21-28 September")
            out.add((d.month, d.day))
            d += timedelta(days=1)
    for text in sources.values():
        out |= {(int(m), int(d)) for _, m, d in re.findall(r"\b(\d{4})-(\d{2})-(\d{2})", text)}
        out |= {(MONTHS[mon[:3].lower()], int(day)) for day, mon, _ in DAY_MONTH.findall(text)}
    return out


def _as_date(value) -> date:
    return (value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))).date()


def _strip_written_dates(text: str, known: set[tuple[int, int]]) -> tuple[str, list[str]]:
    """Remove written dates from the text before the number check; return the ones not known."""
    unknown = []

    def sub(match, day_group, month_group):
        day, month = int(match.group(day_group)), MONTHS[match.group(month_group)[:3].lower()]
        if (month, day) not in known:
            unknown.append(match.group().strip())
        return " "

    text = DAY_MONTH.sub(lambda m: sub(m, 1, 2), text)
    text = MONTH_DAY.sub(lambda m: sub(m, 2, 1), text)
    return text, unknown


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


def check_sentence(s: Sentence, sources: dict[str, str], dates: set[tuple[int, int]] | None = None) -> list[str]:
    """Reasons to remove the sentence (empty list = keep). `dates`: known (month, day)
    pairs; when given, written dates are checked against them instead of as numbers."""
    reasons = []
    if not s.cites:
        reasons.append("cites no finding")
    unknown = [c for c in s.cites if c not in sources]
    if unknown:
        reasons.append(f"cites unknown IDs: {', '.join(unknown)}")
    text = s.text
    if dates is not None:
        text, bad_dates = _strip_written_dates(text, dates)
        if bad_dates:
            reasons.append(f"dates not in the investigation period or the findings: {', '.join(bad_dates)}")
    allowed = [v for c in s.cites if c in sources for v, _ in extract_numbers(sources[c])]
    unsupported = [v for v, d in extract_numbers(text)
                   if not (d == 0 and (v <= 10 or 1900 <= v <= 2100))   # small counts and years are not checked
                   and not _matches(v, d, allowed)]
    if unsupported:
        reasons.append("numbers not found in the cited findings: "
                       + ", ".join(str(int(v)) if v.is_integer() else str(v) for v in unsupported))
    if CAUSAL.search(s.text) and not HEDGE.search(s.text):
        reasons.append("states a cause as fact")
    if PROOF.search(s.text) and not NOT_A_CLAIM.search(s.text):
        reasons.append("claims something is proven or confirmed")
    return reasons
