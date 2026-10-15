"""The only door from agents to data: permission-checked, recorded analytics tools.

* Each agent gets a `AgentTools` object that allows ONLY the tools listed for it
  in TOOL_PERMISSIONS. Asking for anything else raises ToolPermissionError, and the
  denial is recorded. This is enforced by code; no agent (and later no LLM) can
  grant itself more access.
* Every call is recorded as a ToolCall (inputs, size of result, duration).
  Findings cite these call IDs as their evidence.
* Each call opens its own read-only connection from the pool, because LangGraph
  runs parallel agents in separate threads and a connection must not be shared
  between threads.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy import Engine

from payops.agents.schemas import ToolCall
from payops.analytics import queries
from payops.analytics.models import AnalysisFilter

# Which agent may use which tool. The coordinator only reads the dataset window.
TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    "coordinator": frozenset({"dataset_window"}),
    "transaction": frozenset({"summary", "breakdown", "failure_reasons", "sample_failed_transactions"}),
    "pattern": frozenset({"dataset_window", "time_series", "failure_reasons", "events", "sample_failed_transactions"}),
    "forecast": frozenset({"dataset_window", "time_series"}),
    "impact": frozenset({"summary"}),
    "verification": frozenset({"summary", "breakdown", "failure_reasons", "time_series"}),
}


class ToolPermissionError(PermissionError):
    """An agent asked for a tool it is not allowed to use."""


class AgentTools:
    def __init__(self, agent: str, engine: Engine | None):
        if agent not in TOOL_PERMISSIONS:
            raise ToolPermissionError(f"unknown agent '{agent}'")
        self.agent = agent
        self._engine = engine
        self.calls: list[ToolCall] = []

    # ---- tools (thin wrappers around analytics.queries) --------------------------
    def dataset_window(self) -> dict:
        return self._call("dataset_window", {}, lambda c: queries.dataset_window(c))

    def summary(self, f: AnalysisFilter) -> dict:
        return self._call("summary", _describe(f), lambda c: queries.summary(c, f))

    def breakdown(self, f: AnalysisFilter, by: str) -> pd.DataFrame:
        return self._call("breakdown", {**_describe(f), "by": by}, lambda c: queries.breakdown(c, f, by))

    def failure_reasons(self, f: AnalysisFilter) -> pd.DataFrame:
        return self._call("failure_reasons", _describe(f), lambda c: queries.failure_reasons(c, f))

    def time_series(self, f: AnalysisFilter, unit: str, by_provider: bool) -> pd.DataFrame:
        return self._call("time_series", {**_describe(f), "unit": unit, "by_provider": by_provider},
                          lambda c: queries.time_series(c, f, unit, by_provider))

    def events(self, start: datetime, end: datetime) -> tuple[pd.DataFrame, pd.DataFrame]:
        return self._call("events", {"start": start.isoformat(), "end": end.isoformat()},
                          lambda c: queries.events(c, start, end))

    def sample_failed_transactions(self, f: AnalysisFilter, limit: int = 5) -> list[str]:
        return self._call("sample_failed_transactions", {**_describe(f), "limit": limit},
                          lambda c: queries.sample_failed_transactions(c, f, limit))

    @property
    def last_call_id(self) -> str | None:
        return self.calls[-1].call_id if self.calls else None

    def ids_since(self, n: int) -> list[str]:
        """Call IDs made after the first `n` calls (to cite exactly the calls behind a finding)."""
        return [c.call_id for c in self.calls[n:] if c.status == "ok"]

    # ---- plumbing ----------------------------------------------------------------
    def _call(self, tool: str, params: dict[str, Any], fn: Callable[[Any], Any]) -> Any:
        call_id = f"{self.agent}-{len(self.calls) + 1}"
        started = time.perf_counter()
        if tool not in TOOL_PERMISSIONS[self.agent]:
            self.calls.append(ToolCall(call_id=call_id, agent=self.agent, tool=tool, params=params,
                                       duration_ms=0, status="denied", error="not permitted for this agent"))
            raise ToolPermissionError(f"agent '{self.agent}' may not use tool '{tool}'")
        try:
            with self._connection() as conn:
                result = fn(conn)
        except Exception as exc:
            self.calls.append(ToolCall(call_id=call_id, agent=self.agent, tool=tool, params=params,
                                       duration_ms=_ms(started), status="failed", error=type(exc).__name__))
            raise
        self.calls.append(ToolCall(call_id=call_id, agent=self.agent, tool=tool, params=params,
                                   rows=_size(result), duration_ms=_ms(started), status="ok"))
        return result

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        if self._engine is None:  # tests replace the query functions and need no database
            yield None
        else:
            with self._engine.connect() as conn:
                yield conn


def _describe(f: AnalysisFilter) -> dict[str, Any]:
    d: dict[str, Any] = {"start": f.start.isoformat(), "end": f.end.isoformat()}
    if f.provider_codes:
        d["providers"] = list(f.provider_codes)
    if f.payment_methods:
        d["methods"] = list(f.payment_methods)
    if f.failure_reasons:
        d["reasons"] = list(f.failure_reasons)
    return d


def _size(result: Any) -> int | None:
    if isinstance(result, pd.DataFrame):
        return len(result)
    if isinstance(result, tuple):
        return sum(len(x) for x in result if isinstance(x, pd.DataFrame))
    if isinstance(result, list):
        return len(result)
    return 1


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
