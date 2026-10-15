# Analytics Methods

Every number the application shows is computed by deterministic code in `src/payops/analytics/`. The LLM (Phase 5) only explains these results; it never calculates them.

## Three kinds of number

| Kind | Meaning | Example |
|---|---|---|
| **measured** | Counted directly from the data | 536 failed payments in the window |
| **estimate** | Computed from the data with a method that involves a choice | 472.6 *excess* failures, relative to the previous 7 days |
| **hypothetical** | Depends on an assumption you supply | 472.60 USD handling cost, *if* each extra failure costs 1.00 USD |

Every figure carries its kind and a one-line method, so a report can never mix them up silently.

## Definitions

| Metric | Plain-English definition | Formula |
|---|---|---|
| Payment attempts | Payments tried, successful or not | count of rows |
| Failure rate | Share of attempts that failed | failures ÷ attempts (undefined, not 0, when there are no attempts) |
| Change (percentage points) | Difference between two rates: 2.88% → 3.85% is +0.98 points | rate₁ − rate₀ |
| Expected failures | How many would have failed at the old rate | attempts now × old rate |
| Excess failures | Failures beyond what was expected | failures − expected failures |
| Contribution share | Part of the total rise explained by one segment | segment's excess ÷ sum of all positive excesses |
| z-score / p-value | Whether a change is larger than normal random variation. Says nothing about *why*. | two-proportion z-test (pooled) |
| Affected transaction value | Total amount of failed payments. **Not revenue, not loss**: customers often retry, and the company earns only a fee per payment. | sum of failed amounts |
| Excess affected value | Value of the *extra* failed payments | excess × average failed amount |
| Projected additional failures | *If* the excess rate continued unchanged for N hours | excess per hour × N |
| Handling cost scenarios | Illustrative internal cost of the extra failures | excess × assumed cost per failure (low / base / high) |

"Last week" means the last complete Monday-to-Sunday week (UTC) before the as-of time.

## Incident detection baseline (`detection.py`)

1. For each provider and hour: baseline rate = failures ÷ attempts over the 7 days that ended 24 hours earlier.
2. Expected = attempts × baseline rate. z = (actual − expected) ÷ binomial standard deviation.
3. An hour is flagged when z ≥ 4 **and** there are ≥ 5 more failures than expected.
4. Flagged hours ≤ 6 hours apart are merged into one window (traffic is thin at night).
5. A window is reported if it has ≥ 2 flagged hours **and** ≥ 15 excess failures in total.

Why rule 5 exists: with about 2,000 hours per provider, a handful of single hours look extreme purely by chance. Before rule 5, seed 42 produced 6 false alarms like this; after it, none.

**Known limits:** the first 8 days can't be checked; problems shorter than 2 hours or smaller than 15 extra failures are not reported; a slow gradual rise may never cross the threshold.

Measured results are in `docs/evaluation.md`.

## Relating anomalies to events (`events.py`)

Deployments and provider notices from 24 hours before an anomaly until its end are listed with their time offset. Every relationship text states that timing overlap is **not evidence of cause**.

An extra clue: `change_around` compares each provider's failure rate in the 24 hours before and after a deployment, with a significance test. Our own software is shared by all providers. So if only **one** provider got significantly worse after our deployment, a provider-side problem is the more likely explanation. That is still a hypothesis to verify, not a conclusion.

## Forecasting (`forecast.py`)

| Target | Method | Checked against |
|---|---|---|
| Daily attempts | Linear regression: trend + day of week (9 parameters, numpy least squares). Interval ±1.96 × residual standard deviation. | "Same day last week" (seasonal naive), on the last 14 days held out |
| Daily failure rate | Median of the last 28 daily rates; band = 10th–90th percentile | Same naive baseline |

No forecast is produced with fewer than 28 days of history. With 28–41 days, a forecast is given but no backtest is possible, and the result says so. scikit-learn is not used: a 9-parameter linear model doesn't need it, and leaving it out removes a dependency.
