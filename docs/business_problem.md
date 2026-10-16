# The Business Problem

Written for someone who has not worked in payments. The data in this project is synthetic; the problem is real.

## What a payment failure is

When a customer pays online, the merchant (the business selling something) sends the payment to a **payment provider** (a company that connects to card networks and banks). The provider answers *approved* or *failed*. Failures have reasons: the card was declined, the bank's fraud check refused it, the 3-D Secure check wasn't completed, or the provider **timed out** because something on its side was slow or down.

Some failures are normal: a few percent of attempts fail every day. The question operations teams face is when the rate goes *up*.

## Why it matters

- **Lost sales.** A customer whose payment fails may not try again.
- **Support cost.** Failed payments create tickets, retries and refunds.
- **Wrong fixes.** If the team blames the wrong cause, for example its own last software release, it rolls back good work while the real problem continues.

## How it is investigated today, by hand

An analyst has to combine four kinds of evidence:

| Evidence | Question it answers | Where it usually lives |
|---|---|---|
| Transactions | Who failed, where, why, since when? | the payments database |
| Deployments | What did we change? | release notes, CI/CD history |
| Provider notices | What did the provider admit? | status pages, emails |
| History | What is normal for this time of week? | dashboards, memory |

Doing this well takes hours: comparing periods, separating a real change from daily noise, finding *which* provider and *which* reason explain the extra failures, checking whether a deployment really lines up, and estimating the cost. The common shortcut, blaming whatever changed just before, is often wrong.

## What this project does about it

An analyst types a question such as *"Why did the payment failure rate increase last week?"* and gets, in about half a minute on a laptop:

- the size of the change and whether it is statistically significant
- which provider, payment method and failure reason explain the extra failures
- the time window of the anomaly
- the deployments and provider notices nearby, with **ranked hypotheses** that separate correlation from evidence
- a forecast of what normal looks like next week
- the business impact, labelled *measured*, *estimated* or *hypothetical*
- recommended next steps
- a written report in which every sentence cites the findings behind it and has been checked

Six agents share the work (`docs/agent_design.md`). Numbers come from SQL and tested Python; the local LLM only plans and writes.

## The test scenario

The synthetic dataset (`docs/data_dictionary.md`) has 492,996 payment attempts over 90 days (1 Jul – 28 Sep 2026) across four providers. Hidden in it:

| | What was injected | What the system reported (seed 42) |
|---|---|---|
| Primary incident | KESTREL timeouts, 23 Sep 14:00 → 25 Sep 02:00 UTC | KESTREL, TIMEOUT, 23 Sep 14:00 → 25 Sep 01:00 |
| Red herring | Our deployment DEP-0026, 2 h before | "correlation only"; ranked unlikely, because only KESTREL got worse |
| Provider notice | INC-0003, posted late | found, 3 h after the start |
| Secondary incident | ORBIT, 28 Aug 10:00–13:00, small | found in 6 of 8 random datasets (`docs/evaluation.md`) |

The answer key (`ground_truth.json`) is read only by tests and evaluation, never by the application.

## Who uses it

| Role | Typical person | What they do here |
|---|---|---|
| viewer | manager, support lead | dashboards and KPIs |
| analyst | payment operations analyst | runs and resumes their own investigations |
| lead | team lead | sees and resumes everyone's investigations |
| admin | IT / platform owner | manages accounts, reads the audit log; cannot run investigations |

## What it is not

Not a fraud system, not a real-time alerting system, and not connected to real payment providers. It is a portfolio demonstration of how an investigation workflow could be automated safely, run entirely on one computer. `docs/limitations_and_future_work.md` lists what would change for real use.
