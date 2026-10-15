"""Transaction Intelligence Agent.

Question it answers: "What changed, and who/what explains it?"
1. Overall failure rate this period vs the previous one, with a significance test.
2. The same per provider and per payment method, with excess failures and contribution share.
3. For the provider that explains most of the rise: which failure reasons changed,
   plus a few transaction IDs as evidence.

Every number comes from SQL + the tested Phase 3 functions; statements are
built from those numbers by code, so they cannot drift from the data.
"""

from __future__ import annotations

import pandas as pd

from payops.agents.schemas import Evidence, Finding, TransactionFindings
from payops.agents.tools import AgentTools
from payops.analytics.metrics import compare_periods, two_proportion_test
from payops.analytics.models import AnalysisFilter, Figure
from payops.analytics.serialize import to_records

SIGNIFICANCE = 0.01
CONCENTRATED_SHARE = 0.6  # one segment explaining >= 60% of the rise counts as "concentrated"


def run(state: dict, tools: AgentTools) -> dict:
    plan = state["plan"]
    current = AnalysisFilter(start=plan.current_start, end=plan.current_end, provider_codes=plan.provider_codes)
    baseline = current.model_copy(update={"start": plan.baseline_start, "end": plan.baseline_end})
    findings: list[Finding] = []

    # 1. Overall -------------------------------------------------------------------
    n = len(tools.calls)
    cur, base = tools.summary(current), tools.summary(baseline)
    overall_ids = tools.ids_since(n)
    if not cur["attempts"] or not base["attempts"]:
        raise ValueError("No payments in the selected or previous period; nothing to compare.")
    rate, base_rate = cur["failures"] / cur["attempts"], base["failures"] / base["attempts"]
    z, p = two_proportion_test([cur["failures"]], [cur["attempts"]], [base["failures"]], [base["attempts"]])
    significant = bool(p[0] < SIGNIFICANCE)
    overall = [
        Figure(name="attempts", value=cur["attempts"], unit="payments", kind="measured", method="Count of payment attempts."),
        Figure(name="failures", value=cur["failures"], unit="payments", kind="measured", method="Count of FAILED payments."),
        Figure(name="failure_rate", value=round(rate * 100, 3), unit="%", kind="measured", method="Failures / attempts."),
        Figure(name="previous_failure_rate", value=round(base_rate * 100, 3), unit="%", kind="measured",
               method="Failures / attempts in the previous period of equal length."),
        Figure(name="change", value=round((rate - base_rate) * 100, 3), unit="percentage points", kind="measured",
               method="Difference of the two rates."),
        Figure(name="z_score", value=round(float(z[0]), 2), unit="standard deviations", kind="estimate",
               method="Two-proportion z-test; says whether the change exceeds random variation, not why."),
    ]
    findings.append(Finding(
        finding_id="TX-1", agent="transaction", kind="observation", figures=overall,
        statement=(f"The failure rate was {rate:.2%} against {base_rate:.2%} in the previous period "
                   f"({(rate - base_rate) * 100:+.2f} percentage points). This is "
                   f"{'a statistically significant' if significant else 'not a statistically significant'} change "
                   f"(z = {float(z[0]):.2f})."),
        evidence=Evidence(tool_call_ids=overall_ids),
    ))

    # 2. Per provider and per method -----------------------------------------------------
    n = len(tools.calls)
    by_provider = _compare(tools.breakdown(current, "provider"), tools.breakdown(baseline, "provider"), "provider_code")
    provider_ids = tools.ids_since(n)
    n = len(tools.calls)
    by_method = _compare(tools.breakdown(current, "method"), tools.breakdown(baseline, "method"), "payment_method")
    method_ids = tools.ids_since(n)

    top = by_provider[(by_provider["excess_failures"] > 0) & by_provider["significant"]].head(1)
    top_code, top_share, top_reasons = None, None, []
    if len(top):
        row = top.iloc[0]
        top_code, top_share = str(row["provider_code"]), float(row["contribution_share"])
        findings.append(Finding(
            finding_id="TX-2", agent="transaction", kind="observation",
            figures=[
                Figure(name="contribution_share", value=round(top_share * 100, 1), unit="%", kind="estimate",
                       method="Provider's excess failures / all positive excess failures."),
                Figure(name="excess_failures", value=round(float(row["excess_failures"]), 1), unit="payments", kind="estimate",
                       method="Failures minus attempts x previous-period failure rate."),
            ],
            statement=(f"{top_code} explains {top_share:.0%} of the extra failures: its failure rate went from "
                       f"{row['rate_baseline']:.2%} to {row['rate_current']:.2%} "
                       f"({row['excess_failures']:+.0f} failures above what its previous rate would predict)."),
            evidence=Evidence(tool_call_ids=provider_ids),
        ))

        # 3. Reasons at that provider -------------------------------------------------
        n = len(tools.calls)
        cur_p = current.model_copy(update={"provider_codes": (top_code,)})
        base_p = baseline.model_copy(update={"provider_codes": (top_code,)})
        reasons = _reason_changes(tools.failure_reasons(cur_p), tools.failure_reasons(base_p))
        top_reasons = to_records(reasons)
        reason_ids = tools.ids_since(n)
        if len(reasons):
            r = reasons.iloc[0]
            samples = tools.sample_failed_transactions(cur_p.model_copy(update={"failure_reasons": (str(r["failure_reason"]),)}), 5)
            findings.append(Finding(
                finding_id="TX-3", agent="transaction", kind="observation",
                figures=[Figure(name=f"{r['failure_reason'].lower()}_failures", value=int(r["failures_current"]), unit="payments",
                                kind="measured", method=f"Count of {r['failure_reason']} failures at {top_code} in the period.")],
                statement=(f"At {top_code}, {r['failure_reason']} failures went from {int(r['failures_baseline'])} to "
                           f"{int(r['failures_current'])}, the largest change of any failure reason."),
                evidence=Evidence(tool_call_ids=reason_ids + [tools.last_call_id], sample_transaction_ids=samples),
            ))
    else:
        findings.append(Finding(
            finding_id="TX-2", agent="transaction", kind="observation",
            statement="No provider shows a statistically significant increase in failure rate.",
            evidence=Evidence(tool_call_ids=provider_ids),
        ))

    positive = by_method[by_method["excess_failures"] > 0]
    if len(positive):
        m = positive.iloc[0]
        share = float(m["contribution_share"])
        where = (f"concentrated in {m['payment_method']} ({share:.0%} of the extra failures)" if share >= CONCENTRATED_SHARE
                 else f"spread across payment methods (largest: {m['payment_method']}, {share:.0%})")
        findings.append(Finding(
            finding_id="TX-4", agent="transaction", kind="observation",
            statement=f"By payment method, the increase is {where}.",
            evidence=Evidence(tool_call_ids=method_ids),
        ))

    return {"transaction": TransactionFindings(
        overall=overall, by_provider=to_records(by_provider), by_method=to_records(by_method),
        top_provider=top_code, top_provider_share=top_share, top_provider_reasons=top_reasons, findings=findings,
    )}


def _compare(current: pd.DataFrame, baseline: pd.DataFrame, key: str) -> pd.DataFrame:
    empty = pd.DataFrame(columns=[key, "attempts", "failures"])
    return compare_periods(current if len(current) else empty, baseline if len(baseline) else empty, key=key)


def _reason_changes(current: pd.DataFrame, baseline: pd.DataFrame) -> pd.DataFrame:
    cols = ["failure_reason", "failures"]
    cur = (current[cols] if len(current) else pd.DataFrame(columns=cols)).rename(columns={"failures": "failures_current"})
    base = (baseline[cols] if len(baseline) else pd.DataFrame(columns=cols)).rename(columns={"failures": "failures_baseline"})
    out = cur.merge(base, on="failure_reason", how="outer").fillna(0)
    out["change"] = out["failures_current"] - out["failures_baseline"]
    return out.sort_values(["change", "failure_reason"], ascending=[False, True]).reset_index(drop=True)
