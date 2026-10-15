"""The LangGraph workflow that connects the agents.

                        ┌──> transaction ──┐
    START ──> plan ─────┼──> pattern ──────┼──> impact ──> assemble ──> END
                        └──> forecast ─────┘

* plan runs first; transaction, pattern and forecast only need the plan, so they
  run IN PARALLEL (same LangGraph "superstep", separate threads).
* impact waits for all three (a list-form edge = "wait for every one of these").
* Every node is wrapped by `_node()`, which times it, records its tool calls,
  catches errors (so one failed agent gives a partial result instead of a crash)
  and skips a node whose inputs are missing.
* `recursion_limit` caps the number of steps, so the workflow can never loop forever.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from langgraph.graph import END, START, StateGraph
from sqlalchemy import Engine

from payops.agents import coordinator, forecast_agent, impact_agent, pattern_agent, transaction_agent
from payops.agents.schemas import AgentError, TraceEvent
from payops.agents.state import InvestigationState
from payops.agents.tools import AgentTools, ToolPermissionError

log = logging.getLogger("payops.agents")

MAX_STEPS = 20  # the graph needs 6; anything more means something is wrong

# node name -> (agent identity for permissions, function, state keys it needs)
NODES: dict[str, tuple[str, Callable, tuple[str, ...]]] = {
    "plan": ("coordinator", coordinator.plan, ("question",)),
    "transaction": ("transaction", transaction_agent.run, ("plan",)),
    "pattern": ("pattern", pattern_agent.run, ("plan", "dataset")),
    "forecast": ("forecast", forecast_agent.run, ("plan", "dataset")),
    "impact": ("impact", impact_agent.run, ("plan",)),
    "assemble": ("coordinator", coordinator.assemble, ()),
}

SAFE_ERRORS = (ValueError, ToolPermissionError)  # our own messages; safe to show


def _node(name: str, engine: Engine | None) -> Callable[[dict], dict]:
    agent, fn, needs = NODES[name]

    def run(state: dict) -> dict:
        started_at = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        tools = AgentTools(agent, engine)
        missing = [k for k in needs if state.get(k) is None]
        if missing:
            return {"trace": [TraceEvent(agent=name, status="skipped", started_at=started_at, duration_ms=0,
                                         message=f"missing input: {', '.join(missing)}")]}
        try:
            update, status, message, errors = fn(state, tools), "ok", "", []
        except Exception as exc:  # one agent failing must not crash the whole investigation
            message = str(exc) if isinstance(exc, SAFE_ERRORS) else "Unexpected error (details in the server log)."
            log.error("agent failed", extra={"fields": {"agent": name, "error_type": type(exc).__name__}})
            update, status, errors = {}, "failed", [AgentError(agent=name, error_type=type(exc).__name__, message=message)]
        event = TraceEvent(agent=name, status=status, started_at=started_at, duration_ms=int((time.perf_counter() - t0) * 1000),
                           tool_call_ids=[c.call_id for c in tools.calls], message=message)
        return {**update, "trace": [event], "tool_calls": tools.calls, "errors": errors}

    return run


def build_graph(engine: Engine | None):
    builder = StateGraph(InvestigationState)
    for name in NODES:
        builder.add_node(name, _node(name, engine))
    builder.add_edge(START, "plan")
    for name in ("transaction", "pattern", "forecast"):
        builder.add_edge("plan", name)                                   # fan-out: run in parallel
    builder.add_edge(["transaction", "pattern", "forecast"], "impact")   # fan-in: wait for all three
    builder.add_edge("impact", "assemble")
    builder.add_edge("assemble", END)
    return builder.compile()


def run_investigation(engine: Engine | None, question: str, as_of: datetime | None = None,
                      investigation_id: str | None = None) -> dict:
    """Run the whole workflow and return a JSON-ready dict."""
    investigation_id = investigation_id or str(uuid.uuid4())
    t0 = time.perf_counter()
    state = build_graph(engine).invoke(
        {"investigation_id": investigation_id, "question": question, "as_of": as_of,
         "trace": [], "tool_calls": [], "errors": []},
        {"recursion_limit": MAX_STEPS},
    )
    duration_ms = int((time.perf_counter() - t0) * 1000)
    log.info("investigation finished", extra={"fields": {
        "investigation_id": investigation_id, "status": state["result"]["status"], "duration_ms": duration_ms}})
    return {
        "investigation_id": investigation_id,
        "duration_ms": duration_ms,
        **state["result"],
        "trace": [t.model_dump(mode="json") for t in state["trace"]],
        "tool_calls": [c.model_dump(mode="json") for c in state["tool_calls"]],
    }
