"""The LangGraph workflow that connects the agents, with checkpoints and limits.

                        ┌──> transaction ──┐
    START ──> plan ─────┼──> pattern ──────┼──> impact ──> assemble ──> narrate ──> verify ──> finalize ──> END
                        └──> forecast ─────┘

* plan and narrate are the two steps that may use the local LLM (see coordinator.py);
  verify checks everything, including the LLM's words, before finalize chooses the report.
* plan runs first; transaction, pattern and forecast only need the plan, so they
  run IN PARALLEL (same LangGraph "superstep", separate threads).
* impact waits for all three (a list-form edge = "wait for every one of these").
* Every node is wrapped by `_node()`, which times it, records its tool calls,
  catches errors (so one failed agent gives a partial result instead of a crash)
  and skips a node whose inputs are missing.

Phase 6: resilience
* Checkpoints. After every step its output is saved (history store, schema "ops").
  A resumed run REUSES a saved step only if it succeeded AND every step it depends on
  was reused too; everything else runs again. So after a crash in `pattern`, a resume
  reuses plan, transaction and forecast, and re-runs pattern and everything after it.
  A report written without the LLM because the LLM was down is retried on resume.
* Limits. `recursion_limit` caps the number of steps (no endless loops). A wall-clock
  budget (INVESTIGATION_TIMEOUT_SECONDS) is checked before each expensive step; when
  it is spent, the remaining analysis steps are skipped and the user still gets a
  labelled report. Each agent has a tool-call limit (tools.py) and the LLM a call budget.
* Saving is best effort: if the history database is down, the investigation still
  runs and answers, it just reports `persisted: false`.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, START, StateGraph
from sqlalchemy import Engine

from payops.agents import coordinator, forecast_agent, impact_agent, pattern_agent, transaction_agent, verification_agent
from payops.agents.schemas import AgentError, TraceEvent
from payops.agents.state import InvestigationState
from payops.agents.tools import AgentTools, ToolLimitError, ToolPermissionError
from payops.history.codec import decode_update, encode_json, encode_update
from payops.history.store import InvestigationStore, NotResumable, RESUMABLE, StepRecord
from payops.llm.client import LLMClient

log = logging.getLogger("payops.agents")

MAX_STEPS = 20  # the graph needs 9; anything more means something is wrong
DEFAULT_TIMEOUT_SECONDS = 600.0

# node name -> (agent identity for permissions, function, state keys it needs)
NODES: dict[str, tuple[str, Callable, tuple[str, ...]]] = {
    "plan": ("coordinator", coordinator.plan, ("question",)),
    "transaction": ("transaction", transaction_agent.run, ("plan",)),
    "pattern": ("pattern", pattern_agent.run, ("plan", "dataset")),
    "forecast": ("forecast", forecast_agent.run, ("plan", "dataset")),
    "impact": ("impact", impact_agent.run, ("plan",)),
    "assemble": ("coordinator", coordinator.assemble, ()),
    "narrate": ("coordinator", coordinator.narrate, ("result",)),
    "verify": ("verification", verification_agent.run, ("result",)),
    "finalize": ("coordinator", coordinator.finalize, ("result",)),
}
# Which earlier steps each step's output depends on (used to decide what a resume may reuse).
UPSTREAM: dict[str, tuple[str, ...]] = {
    "plan": (), "transaction": ("plan",), "pattern": ("plan",), "forecast": ("plan",),
    "impact": ("transaction", "pattern", "forecast"), "assemble": ("impact",),
    "narrate": ("assemble",), "verify": ("narrate",), "finalize": ("verify",),
}
LLM_NODES = {"plan", "narrate"}   # the only steps that receive the LLM client
# Steps skipped once the time budget is spent. assemble, verify and finalize are quick
# and always run, so the user always gets a (labelled) report.
TIME_LIMITED = {"plan", "transaction", "pattern", "forecast", "impact", "narrate"}

SAFE_ERRORS = (ValueError, ToolPermissionError, ToolLimitError)  # our own messages; safe to show
_now = time.monotonic   # the clock for the time budget (tests replace it)


@dataclass
class RunContext:
    """Per-run settings the node wrappers share (read-mostly; safe across parallel threads)."""

    investigation_id: str
    run: int = 1
    deadline: float = float("inf")                         # a _now() value
    store: InvestigationStore | None = None
    reuse: dict[str, dict[str, Any]] = field(default_factory=dict)   # step -> saved update to replay
    save_failures: int = 0

    def save(self, fn: Callable[[], None]) -> None:
        if self.store is None:
            return
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - history is best effort; the investigation goes on
            self.save_failures += 1
            log.warning("history write failed", extra={"fields": {"investigation_id": self.investigation_id,
                                                                   "error_type": type(exc).__name__}})


def _reusable(name: str, status: str, update: dict) -> bool:
    if status != "ok":
        return False
    if name == "narrate" and update.get("narrative") is None and \
            str(update.get("narrative_note", "")).startswith(coordinator.LLM_FAILURE_NOTE):
        return False                       # the LLM was down: try it again on resume
    return True


def _node(name: str, engine: Engine | None, llm: LLMClient | None = None, ctx: RunContext | None = None
          ) -> Callable[[dict], dict]:
    agent, fn, needs = NODES[name]
    ctx = ctx or RunContext(investigation_id="unsaved")

    def run(state: dict) -> dict:
        if name in ctx.reuse:   # resumed run: replay the saved output instead of running the step again
            update = decode_update(ctx.reuse[name])
            update["trace"] = [t.model_copy(update={"from_checkpoint": True}) for t in update.get("trace", [])]
            return update

        started_at = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        tools = AgentTools(agent, engine)
        missing = [k for k in needs if state.get(k) is None]
        if missing:
            update = _skip(name, started_at, f"an earlier step it depends on did not finish (missing: {', '.join(missing)})")
        elif name in TIME_LIMITED and _now() > ctx.deadline:
            update = _skip(name, started_at, "time limit for the investigation reached")
            if name == "narrate":
                update["narrative"], update["narrative_note"] = None, "time limit reached before the report could be written"
        else:
            try:
                update = fn(state, tools, llm) if name in LLM_NODES else fn(state, tools)
                status, message, errors = "ok", "", []
            except Exception as exc:  # one agent failing must not crash the whole investigation
                message = str(exc) if isinstance(exc, SAFE_ERRORS) else "Unexpected error (details in the server log)."
                log.error("agent failed", extra={"fields": {"agent": name, "error_type": type(exc).__name__}})
                update, status, errors = {}, "failed", [AgentError(agent=name, error_type=type(exc).__name__, message=message)]
            event = TraceEvent(agent=name, status=status, started_at=started_at,
                               duration_ms=int((time.perf_counter() - t0) * 1000),
                               tool_call_ids=[c.call_id for c in tools.calls], message=message)
            update = {**update, "trace": [event], "tool_calls": tools.calls, "errors": errors}

        event = update["trace"][0]
        ctx.save(lambda: ctx.store.save_step(  # type: ignore[union-attr]
            ctx.investigation_id, name, ctx.run, event.status, _reusable(name, event.status, update),
            encode_update(update), event.duration_ms))
        return update

    return run


def _skip(name: str, started_at: datetime, message: str) -> dict:
    return {"trace": [TraceEvent(agent=name, status="skipped", started_at=started_at, duration_ms=0, message=message)],
            "tool_calls": [], "errors": []}


def build_graph(engine: Engine | None, llm: LLMClient | None = None, ctx: RunContext | None = None):
    builder = StateGraph(InvestigationState)
    for name in NODES:
        builder.add_node(name, _node(name, engine, llm, ctx))
    builder.add_edge(START, "plan")
    for name in ("transaction", "pattern", "forecast"):
        builder.add_edge("plan", name)                                   # fan-out: run in parallel
    builder.add_edge(["transaction", "pattern", "forecast"], "impact")   # fan-in: wait for all three
    builder.add_edge("impact", "assemble")
    builder.add_edge("assemble", "narrate")
    builder.add_edge("narrate", "verify")
    builder.add_edge("verify", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


def _execute(engine: Engine | None, llm: LLMClient | None, ctx: RunContext, initial: dict) -> dict:
    return build_graph(engine, llm, ctx).invoke(initial, {"recursion_limit": MAX_STEPS})


# ---- public entry points ---------------------------------------------------------------------

def run_investigation(engine: Engine | None, question: str, as_of: datetime | None = None,
                      investigation_id: str | None = None, llm: LLMClient | None = None,
                      store: InvestigationStore | None = None, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Run the whole workflow and return a JSON-ready dict.

    llm=None runs without the LLM (template report, deterministic plan). Pass a fresh
    client per investigation so each one gets its own call budget.
    store=None runs without saving (nothing can be resumed later).
    """
    ctx = RunContext(investigation_id=investigation_id or str(uuid.uuid4()), store=store)
    ctx.save(lambda: store.create(ctx.investigation_id, question, as_of, llm is not None))  # type: ignore[union-attr]
    if ctx.save_failures:
        ctx.store = None          # could not even create the record: don't try to save steps
    return _run(engine, llm, ctx, question, as_of, timeout_seconds)


def resume_investigation(engine: Engine | None, store: InvestigationStore, investigation_id: str,
                         make_llm: Callable[[], LLMClient] | None = None,
                         timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Continue a partial or interrupted investigation from its checkpoints.

    Raises NotFound, or NotResumable with a message for the user (complete, already
    running, or the data was reloaded since the investigation started).
    """
    record = store.get(investigation_id)
    if record.status not in RESUMABLE:
        raise store._not_resumable(record.status)
    saved = latest_reusable(store.steps(investigation_id))
    if "plan" in saved:   # checkpoints only make sense against the same data
        then = saved["plan"]["dataset"]["dataset_id"]
        now = AgentTools("coordinator", engine).dataset_window()["dataset_id"]
        if then != now:
            raise NotResumable(f"The data was reloaded since this investigation ran (dataset {then}, now {now}). "
                               "Start a new investigation instead.")
    record = store.claim_for_resume(investigation_id)          # atomic: two resumes cannot both run
    reuse = reusable_steps(saved)
    llm = make_llm() if (record.use_llm and make_llm is not None) else None
    ctx = RunContext(investigation_id=investigation_id, run=record.runs, store=store, reuse=reuse)
    log.info("investigation resumed", extra={"fields": {"investigation_id": investigation_id, "run": record.runs,
                                                        "reused_steps": sorted(reuse)}})
    return _run(engine, llm, ctx, record.question, record.as_of, timeout_seconds)


def latest_reusable(steps: list[StepRecord]) -> dict[str, dict[str, Any]]:
    """step -> payload of its most recent successful, reusable run."""
    out: dict[str, dict[str, Any]] = {}
    for s in sorted(steps, key=lambda s: s.run):
        if s.status == "ok" and s.reusable:
            out[s.step] = s.payload
        elif s.step in out:          # a later run of this step did not succeed: its old output is stale
            del out[s.step]
    return out


def reusable_steps(saved: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Keep a saved step only if every step it depends on is kept too (NODES is in dependency order)."""
    keep: dict[str, dict[str, Any]] = {}
    for name in NODES:
        if name in saved and all(u in keep for u in UPSTREAM[name]):
            keep[name] = saved[name]
    return keep


def _run(engine, llm, ctx: RunContext, question: str, as_of: datetime | None, timeout_seconds: float) -> dict:
    t0 = time.perf_counter()
    ctx.deadline = _now() + timeout_seconds
    initial = {"investigation_id": ctx.investigation_id, "question": question, "as_of": as_of,
               "trace": [], "tool_calls": [], "errors": [], "llm_calls": []}
    try:
        state = _execute(engine, llm, ctx, initial)
    except BaseException:
        # Ctrl+C, a crash inside LangGraph itself, the server shutting down...: record it so the
        # investigation can be resumed. (A power cut is caught later by the heartbeat check.)
        ctx.save(lambda: ctx.store.mark_interrupted(ctx.investigation_id))  # type: ignore[union-attr]
        raise
    duration_ms = int((time.perf_counter() - t0) * 1000)
    if "report_text" not in state["result"]:  # finalize itself failed: still give the user a readable report
        from payops.agents.template_report import render

        state["result"].update(mode="template", mode_reason="Template report used: the final step failed.")
        state["result"]["report_text"] = render(state["result"])
    reused = sorted(n for n in NODES if n in ctx.reuse)
    result = {
        "investigation_id": ctx.investigation_id,
        "question": question,
        "duration_ms": duration_ms,
        **state["result"],
        "trace": [t.model_dump(mode="json") for t in state["trace"]],
        "tool_calls": [c.model_dump(mode="json") for c in state["tool_calls"]],
        "llm_calls": [c.model_dump(mode="json") for c in state.get("llm_calls", [])],
        "llm_model": llm.model if llm is not None else None,
        "run": ctx.run,
        "reused_steps": reused,
    }
    dataset = state.get("dataset") or {}
    ctx.save(lambda: ctx.store.finish(  # type: ignore[union-attr]
        ctx.investigation_id, result["status"], result.get("mode"), result.get("mode_reason"), result.get("headline"),
        dataset.get("dataset_id"), duration_ms, encode_json({**result, "persisted": ctx.save_failures == 0})))
    result["persisted"] = ctx.store is not None and ctx.save_failures == 0
    log.info("investigation finished", extra={"fields": {
        "investigation_id": ctx.investigation_id, "status": result["status"], "mode": result.get("mode"),
        "duration_ms": duration_ms, "run": ctx.run, "reused_steps": reused, "persisted": result["persisted"]}})
    return result
