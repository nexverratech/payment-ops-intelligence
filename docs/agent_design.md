# Agent Design

## Why several agents, and where a simple program would be better

An investigation has distinct jobs: measure the change, find when and where it happened, look at what happened around it, size the impact, and say what to expect next. Splitting them into agents gives each job:
- its own inputs and outputs (typed schemas)
- its own data permissions
- its own place in the trace

When one fails, the others still deliver a partial result.

**Honest trade-off:** in Phase 5a every agent is deterministic code. A plain Python script calling the same functions in order would give identical numbers, so for scheduled weekly reporting that would be the simpler, better choice. The workflow framework earns its place in three ways:
- **parallel steps:** transaction, pattern and forecast run at the same time
- **explicit, inspectable state with a trace**
- **checkpointing and resumable runs (Phase 6)**

The LLM (Phase 5b) adds two things on top: turning a free-text question into a plan, and writing a readable narrative from the computed findings. It never computes numbers.

## Workflow

```
                        ┌──> transaction ──┐
    START ──> plan ─────┼──> pattern ──────┼──> impact ──> assemble ──> END
                        └──> forecast ─────┘
```

| Step | Agent | Needs | Produces | Allowed tools |
|---|---|---|---|---|
| plan | Operations Coordinator | question | dataset window, `Plan` (period, baseline, scope, steps) | `dataset_window` |
| transaction | Transaction Intelligence | plan | overall change + z-test, per provider / method comparison, top contributor and its failure reasons, sample transaction IDs | `summary`, `breakdown`, `failure_reasons`, `sample_failed_transactions` |
| pattern | Pattern Investigation | plan | anomaly windows with dominant reason, related deployments and notices, ranked hypotheses | `dataset_window`, `time_series`, `failure_reasons`, `events`, `sample_failed_transactions` |
| forecast | Forecasting | plan | next-7-day volume and normal failure rate, backtest vs naive baseline | `dataset_window`, `time_series` |
| impact | Business Impact | plan, and pattern or transaction | measured / estimate / hypothetical figures | `summary` |
| assemble | Operations Coordinator | everything available | final result: headline, findings, hypotheses, uncertainties, failed/skipped steps | none (reads state only) |

**Parallel execution:** transaction, pattern and forecast only need the plan, so LangGraph runs them in the same step on separate threads. Each tool call opens its own connection from the read-only pool, because a database connection must not be shared between threads.

**Shared state** (`agents/state.py`): one dictionary passed between steps. Each agent returns only the keys it changes. `trace`, `tool_calls` and `errors` are append-only lists (reducers), so parallel agents can add to them at the same moment without overwriting each other.

## Safety and control rules (enforced by code)

| Rule | Where |
|---|---|
| Agents reach data only through `AgentTools`, never directly | `agents/tools.py` |
| Each agent has a fixed tool allowlist; anything else raises `ToolPermissionError` and is recorded as `denied` | `TOOL_PERMISSIONS` in `agents/tools.py` |
| Tools use the **read-only** database account; the database itself refuses writes | `db/connection.get_readonly_engine` |
| Every tool call is recorded (inputs, result size, duration); every finding cites the calls behind it | `ToolCall`, `Evidence` in `agents/schemas.py` |
| One failing agent → its step is marked `failed`, later steps that need it are `skipped` or fall back, and the result is `partial` | `_node()` in `agents/graph.py` |
| Error messages from unexpected failures are replaced with a generic message, so no SQL or internals leak | `SAFE_ERRORS` in `agents/graph.py` |
| A hard step limit (`recursion_limit = 20`; the graph needs 6) stops runaway loops | `MAX_STEPS` in `agents/graph.py` |

## Hypothesis ranking (Pattern agent)

Hypotheses get transparent evidence points. Every point appears in the output as a "supporting" or "contradicting" line, so a reviewer can check the reasoning.

| Hypothesis | Evidence rule | Points |
|---|---|---|
| Provider-side problem at P | only P shows an anomaly in the period (others too: −1) | +2 |
| | most failures in the window are technical (TIMEOUT, PROVIDER_ERROR) | +1 |
| | P published a status notice overlapping the anomaly | +2 |
| | after a nearby deployment, only P got significantly worse | +1 |
| Our deployment D | D happened within 24 h before the anomaly | +1 |
| | ≥ 2 providers got significantly worse after D (shared cause) | +2 |
| | only one provider got worse after D | −1 |
| Customer-side change | most failures are customer declines (e.g. INSUFFICIENT_FUNDS) | +2 |

Status:
- the top hypothesis with score ≥ 2 is **"leading (unconfirmed)"**
- a score ≤ 0 is **"unlikely"**
- the rest are **"alternative"**

**Nothing is ever labelled a root cause.** Each hypothesis lists next checks that could confirm it, for example the provider's incident report.

Result on the default dataset: provider-side at KESTREL scores 6 (leading). The checkout deployment scores 0 (unlikely), because only KESTREL got worse after it although our software serves all four providers. This matches the ground truth.

## Output contract

`run_investigation()` returns a JSON-ready dict:
- `status`: `complete` / `partial`
- `headline`, `plan`
- `findings`: each with `finding_id`, `agent`, `kind`, `statement`, `figures`, `evidence`
- `hypotheses`, `anomalies`, `impact_basis`, `forecast`
- `uncertainties`, `failed_steps`, `skipped_steps`, `errors`
- `trace`: agent, status, duration, tool calls
- `tool_calls`: full records
- `duration_ms`

`agents/template_report.py` turns it into text without an LLM, and labels it so.
