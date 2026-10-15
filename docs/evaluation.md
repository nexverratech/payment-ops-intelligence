# Evaluation

Results here are **actual runs**, each labelled with how it was produced. Sections are added as phases complete. Nothing is estimated or copied from elsewhere.

## Detection baseline (Phase 3)

**What is measured**
- **Precision** = true alarms ÷ all alarms: when the system raises an alarm, how often is it right?
- **Recall** = incidents found ÷ incidents injected: how many real problems does it catch?

A detected window counts as a true alarm when it is on the right provider and overlaps an injected incident in time.

**How it was run:** `python -m payops.analytics.detection_eval --seeds 42 1 2 3 4 5 6 7`. Each seed is a fresh 90-day dataset with two injected incidents: a large 36-hour Kestrel timeout incident ("primary") and a small 3-hour Orbit error incident ("secondary").

**Result (run on 15 Oct 2026, NumPy 2.5.3 / pandas 3.0.5):**

| Seed | Alarms | False alarms | Primary | Secondary |
|---|---|---|---|---|
| 42 | 2 | 0 | found | found |
| 1 | 2 | 0 | found | found |
| 2 | 2 | 0 | found | found |
| 3 | 2 | 0 | found | found |
| 4 | 1 | 0 | found | missed |
| 5 | 2 | 0 | found | found |
| 6 | 2 | 0 | found | found |
| 7 | 1 | 0 | found | missed |

**Precision 14/14 = 100%. Recall 14/16 = 87.5%** (primary 8/8, secondary 6/8).

**Honest caveats**
- The thresholds were tuned while looking at seed 42 only. Seeds 1–7 were not used for tuning; on those seeds alone, precision is 12/12 and recall 12/14.
- 8 seeds is a small sample; these figures are indicative, not precise.
- The data is synthetic and the incidents are cleaner than real ones. Real-world precision would be lower.
- The secondary incident is deliberately small; missing it in 2 of 8 seeds is the documented limit "fewer than 15 extra failures are not reported" in action.

## Deterministic report on the default dataset (seed 42)

`python -m payops.analytics.report`, as of the end of the data (29 Sep 2026 00:00 UTC):

| Question | Answer from the deterministic pipeline | Ground truth |
|---|---|---|
| Did the failure rate rise last week? | Yes: 2.88% → 3.85% (z = 7.75) | Yes |
| Which provider? | KESTREL, 98% of the extra failures | KESTREL |
| Which failure reason? | TIMEOUT (30 → 480) | TIMEOUT |
| When? | 23 Sep 14:00 → 25 Sep 01:00 UTC | 23 Sep 14:00 → 25 Sep 02:00 |
| Deployment before it? | DEP-0026, 2.0 h before, **correlation only**; after it only KESTREL rose significantly | DEP-0026 correlated, not causal |
| Provider notice? | INC-0003, posted 3.0 h after the start | INC-0003 |

**Forecast backtest (14 held-out days):**
- Volume: model error 51 payments/day vs the naive baseline's 118. The model beats the baseline.
- Failure rate: model error 0.76 vs 0.88 percentage points. The model beats the baseline, but both are distorted by the incident days in the holdout.

## LLM report verification (Phase 5b)

**Metrics:**
- **Evidence support rate** = LLM sentences kept by verification ÷ sentences written: how much of what the model writes is backed by computed findings?
- **Unsupported-claim rate** = sentences removed ÷ sentences written.

**Verification behaviour, checked with a scripted LLM** (run 15 Oct 2026, real dataset, seed 42): a report with 10 sentences, 3 of them deliberately wrong.

| Planted problem | Caught? | Reason given |
|---|---|---|
| "The checkout deployment DEP-0026 caused the timeouts." | ✅ removed | states a cause as fact |
| "Overall about 9,999 payments were affected." | ✅ removed | number not found in the cited finding |
| "This shows the outage cost the company money." (no citation) | ✅ removed | cites no finding |
| 7 faithful sentences (rates, shares, window, impact, next step) | ✅ all kept | — |

All recomputation, integrity and policy checks passed. Support rate 0.7, unsupported-claim rate 0.3.

### First run with the real model, and what it taught us

Run on 15 Oct 2026 on the developer's machine (`qwen3:4b`, AMD GPU), question "What went wrong with Kestrel in the week of 21 September 2026?". The model planned correctly (scope KESTREL, week of 21 Sep). It wrote 11 sentences; the first version of the checker removed 4 (36%), so the template report was used.

All other checks passed: both recomputed rates, the anomaly window (536/2,323), the affected value (47,233.12), evidence integrity, tool permissions and the LLM call budget.

Reviewing the 4 removed sentences by hand showed that **all four were true**, so these were checker errors, not model errors:

| Removed sentence (shortened) | Reason given | Actual problem | Fix |
|---|---|---|---|
| "…from 2.94% to 7.27% in the week of 21 September 2026…" (written twice) | number 21 not in TX-1 | A date was checked as if it were a count; the model also repeated the sentence | Written dates are checked against the investigated period and the dates in the findings; exact repeats are dropped and not counted |
| "The leading hypothesis (not confirmed)… 87% of failures being TIMEOUT…" | number 87 not in the cited findings | Most likely cited PT-3, the finding that summarises H-PROVIDER, whose evidence holds the 87% (inferred: this run's output did not include citations) | Citing a leading-hypothesis finding covers that hypothesis's evidence; removed sentences now show their citations |
| "Ask KESTREL for their incident report… to confirm the provider-side problem." | claims something is proven | "to confirm" is an intention, not a claim | Only claim forms ("confirmed", "proves", "definitely") are flagged, and not after "not", "until", "should" etc. |

Each case is now a regression test in `tests/test_verification.py`, beside tests showing the checks still catch invented numbers, unknown dates, numbers borrowed from uncited findings, and real proof claims. Replaying the four sentences on the seed-42 data with one planted bad sentence: the four pass, the repeat is dropped, the planted sentence is removed.

The lesson: a verifier has false positives too. They are found the same way as model errors, by reading what was removed, and every change is pinned by a test so the checker isn't loosened by accident.

### After the fix (same machine, same question, 15 Oct 2026)

Two runs (the CLI and the live test), both **`mode: llm`, 13 of 13 sentences kept**, all recomputation and policy checks passed. Latency: about 17 s end to end (planning 1.5 s, writing the report 15.6 s, all analytics and verification under 0.5 s).

**What verification cannot catch.** One kept sentence reads: "If the excess failure rate continued for 24 more hours, about 324 additional payments would fail, costing between 118.15 and 1,417.80 USD." Every number is copied correctly from IM-3, but in IM-3 the 118.15–1,417.80 USD range is the handling cost of the extra failures *so far*, not of the next 24 hours. Number-matching checks that a number exists in the cited finding, not that it keeps its meaning. This is why the report states that numbers come from code, keeps the [hypothetical] label, and shows the cited findings beside every sentence. Measuring how often this happens needs human review of a sample of reports (Phase 7).

**Not yet measured:** support rate and unsupported-claim rate for the **real** local model (`qwen3:4b`) over a set of questions. That evaluation, with latency, is part of Phase 7.
