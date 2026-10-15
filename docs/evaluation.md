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
