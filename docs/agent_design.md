# Agent Design

## Why several agents, and where a simple program would be better

An investigation has distinct jobs: measure the change, find when and where it happened, look at what happened around it, size the impact, and say what to expect next. Splitting them into agents gives each job:
- its own inputs and outputs (typed schemas)
- its own data permissions
- its own place in the trace

When one fails, the others still deliver a partial result.

**Honest trade-off:** the analysis agents are deterministic code. A plain Python script calling the same functions in order would give identical numbers, so for scheduled weekly reporting that would be the simpler, better choice. The workflow framework earns its place in three ways:
- **parallel steps:** transaction, pattern and forecast run at the same time
- **explicit, inspectable state with a trace**
- **checkpointing and resumable runs (Phase 6)**

The local LLM adds two things on top: turning a free-text question into a plan, and writing a readable narrative from the computed findings. It never computes numbers, never touches the data, and everything it returns is checked by code.

## Workflow

```
                        ┌──> transaction ──┐
    START ──> plan ─────┼──> pattern ──────┼──> impact ──> assemble ──> narrate ──> verify ──> finalize ──> END
              (LLM)     └──> forecast ─────┘                            (LLM)
```

| Step | Agent | Needs | Produces | Allowed tools |
|---|---|---|---|---|
| plan | Operations Coordinator | question | dataset window, `Plan` (period, baseline, scope, steps) | `dataset_window` |
| transaction | Transaction Intelligence | plan | overall change + z-test, per provider / method comparison, top contributor and its failure reasons, sample transaction IDs | `summary`, `breakdown`, `failure_reasons`, `sample_failed_transactions` |
| pattern | Pattern Investigation | plan | anomaly windows with dominant reason, related deployments and notices, ranked hypotheses | `dataset_window`, `time_series`, `failure_reasons`, `events`, `sample_failed_transactions` |
| forecast | Forecasting | plan | next-7-day volume and normal failure rate, backtest vs naive baseline | `dataset_window`, `time_series` |
| impact | Business Impact | plan, and pattern or transaction | measured / estimate / hypothetical figures | `summary` |
| assemble | Operations Coordinator | everything available | result: headline, findings, hypotheses, uncertainties, failed/skipped steps | none (reads state only) |
| narrate | Operations Coordinator + **local LLM** | result | `NarrativeReport`: sentences, each citing finding/hypothesis IDs | none (LLM gets only the computed findings) |
| verify | Verification and Policy | result, narrative | `VerificationReport`: recomputations, integrity and policy checks, removed sentences with reasons, decision | `summary`, `breakdown`, `failure_reasons`, `time_series` |
| finalize | Operations Coordinator | result, verification | `mode` (`llm` or `template`), reason, report text | none |

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

## The local LLM: what it may and may not do

| | Planner (plan step) | Narrator (narrate step) |
|---|---|---|
| Input | the question (inside `<question>` tags), data window, provider list | computed findings, hypotheses, uncertainties (inside `<data>` tags) |
| Output | `PlanProposal` JSON: a date in the week, provider codes, one-line reasoning | `NarrativeReport` JSON: sentences, each with `cites` |
| Checked by | `validate_proposal()`: known provider codes only; complete week; previous week available | the Verification agent, sentence by sentence |
| If invalid or unavailable | default weekly review, and the plan says why | template report, labelled with the reason |

**Client limits** (`llm/client.py`):
- only 127.0.0.1
- a timeout per call (`LLM_TIMEOUT_SECONDS`)
- at most `LLM_MAX_CALLS` calls per investigation
- JSON schema enforced by Ollama (`format`) and validated again by Pydantic, with one retry
- `think: false`, temperature 0, fixed seed

**Prompt-injection awareness:** user text and data text are passed as data inside tags, and the model is told to ignore instructions inside them. That alone is not a defence; these are:
1. The model has no tools and no data access.
2. Its output must match a narrow schema.
3. Every field is validated by code: an injected provider code like `ALL'; SELECT ...` is simply rejected.
4. Every sentence it writes is checked against computed findings.

`tests/test_agent_graph_llm.py` includes an injection attempt.

## Verification and Policy Agent

| Check | What it does | On failure |
|---|---|---|
| `recompute_current_failure_rate`, `recompute_previous_failure_rate` | Re-queries the database and compares with the Transaction agent's figures | template report |
| `recompute_anomaly_window`, `recompute_affected_value` | Re-counts the main anomaly window and its failed value | template report |
| `evidence_integrity` | Every finding cites tool calls that exist and succeeded | template report |
| `tool_permissions` | No denied calls; every call within the agent's allowlist | template report |
| `llm_call_budget` | No LLM call over budget | template report |
| Per LLM sentence | Exact repeats of an earlier sentence are dropped first and not counted. Then each sentence must cite known IDs. Every number must appear in the cited material (rounding allowed; counts ≤ 10 and years not checked). A finding that summarises the leading hypothesis (e.g. PT-3) also covers that hypothesis's evidence. Written dates ("21 September") must be a day of the investigated or comparison week, or a date in the findings. No unhedged causal wording ("caused", "root cause", "due to"...). No claims that something *is* proven or confirmed; "to confirm…" in a next step is an intention, not a claim. | sentence removed and listed with its citations and reason |
| Overall | More than 30% of sentences removed, or no summary left | template report, followed by a "WHY THE LLM REPORT WAS NOT USED" section listing every removed sentence |

**Metrics** (recorded per investigation, reported in Phase 7):
- **evidence support rate** = sentences kept ÷ sentences written
- **unsupported-claim rate** = sentences removed ÷ sentences written

## Resilience: checkpoints, resume and limits (Phase 6)

**Checkpoints.** After every step, the node wrapper (`_node` in `graph.py`) saves the step's output to `ops.investigation_steps`: its state update as JSON, plus status, run number and a *reusable* flag. Agents never see the store; only the runner writes it, through the history login.

**Resume rule.** A resumed run replays a saved step only if:
- the step succeeded and is marked reusable, and
- every step it depends on (`UPSTREAM` in `graph.py`) is replayed too.

Everything else runs again. Examples:

| What happened | Replayed on resume | Re-run |
|---|---|---|
| `pattern` crashed | plan, transaction, forecast | pattern, impact, assemble, narrate, verify, finalize |
| Process stopped during `impact` | plan, transaction, pattern, forecast | impact onward |
| Time ran out after `plan` | plan | everything else |
| Report written without the LLM because the LLM was down | that step is marked not reusable | narrate onward |

**Safety:**
- A replayed step's trace event is marked `from_checkpoint`, and its tool calls keep their original IDs, so evidence stays traceable.
- Before resuming, the dataset ID saved with the plan is compared with the loaded data. If it changed, the resume is refused.
- Claiming a run for resume is one atomic `UPDATE ... WHERE status IN ('partial', 'interrupted')`, so two resumes can't run the same investigation at once.

**Why not LangGraph's own checkpointer?** LangGraph has one (`langgraph-checkpoint-postgres`, MIT). We chose step checkpoints in our own tables because:

1. **No new dependency.** Every addition needs its licence, network behaviour and tests checked; this needs none.
2. **Safer loading.** LangGraph's loader rebuilds Python objects from msgpack, and that path had a code-execution flaw (CVE-2026-28277, fixed in langgraph 1.0.10). Ours only *validates* JSON against `InvestigationState`, so stored data can't choose which class gets created (`history/codec.py`, tested).
3. **Readable and permissioned.** Checkpoints are JSON rows an analyst can read with SQL, in a schema with its own least-privilege login.
4. **Finer reuse.** Reuse is decided per step (finer than LangGraph's superstep boundaries), with a rule we can test directly.

The trade-off: we don't get LangGraph features built on its checkpointer, such as human-in-the-loop interrupts and time-travel debugging. Neither is needed here.

**Limits:**

| Limit | Default | Where | When exceeded |
|---|---|---|---|
| Steps per run | 20 (9 needed) | `MAX_STEPS`, LangGraph `recursion_limit` | LangGraph error; run recorded `interrupted` |
| Time per investigation | 600 s | `INVESTIGATION_TIMEOUT_SECONDS`, checked before each analysis step and the report | Remaining steps skipped; labelled partial report; resumable |
| Tool calls per agent per step | 30 (busiest uses 9) | `MAX_CALLS_PER_STEP` in `tools.py` | That step fails with a clear message |
| Query time | 30 s (read-only), 10 s (history) | `statement_timeout` on each login | Query stopped; not retried |
| LLM calls per investigation | 4 | `LLM_MAX_CALLS` | Template report |
| One LLM reply | 180 s | `LLM_TIMEOUT_SECONDS` | Template report (and the report is retried on resume) |
| Concurrent investigations | 2 | `MAX_CONCURRENT_INVESTIGATIONS` | HTTP 429 "try again" |

**Retries.** A dropped database connection or server restart (SQLAlchemy `OperationalError`, invalidated connection) is retried once after 0.5 s. Reads are safe to repeat. A statement timeout and real SQL errors are not retried. The LLM client keeps its own rule from Phase 5b: one retry for invalid output.

**Best-effort history.** If saving fails, the investigation still completes and returns `persisted: false`. Losing the answer because the audit store hiccuped would be the worse outcome, and the failure is logged.

## Who may start, see or resume an investigation (Phase 7)

The agents run with the same read-only database login and tool allowlists as before. What changed is *who* may trigger them:
- The API checks the signed-in user's role and, for an existing investigation, its owner, before the workflow starts (docs/security.md).
- The LLM is never asked whether something is allowed, and the user's identity never enters a prompt.
- An injected instruction in a question can at most change the proposed plan. Code validates that proposal against the known providers and the data window (evaluation cases 7–8).
