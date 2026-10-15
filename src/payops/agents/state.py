"""The shared workflow state: one dictionary that LangGraph passes between agents.

Each agent returns only the keys it changes. Keys marked with a reducer
(`Annotated[list, operator.add]`) are APPENDED to instead of replaced. That
matters when agents run in parallel: three agents can each add their trace
events and tool calls at the same moment without overwriting each other.
"""

from __future__ import annotations

import operator
from datetime import datetime
from typing import Annotated, Any

# typing_extensions, not typing: pydantic validates these TypedDicts (history/codec.py) and,
# on Python < 3.12, only accepts the typing_extensions version.
from typing_extensions import TypedDict

from payops.agents.schemas import (
    AgentError,
    ForecastFindings,
    ImpactFindings,
    PatternFindings,
    Plan,
    ToolCall,
    TraceEvent,
    TransactionFindings,
    VerificationReport,
    NarrativeReport,
)
from payops.llm.client import LLMCall


class DatasetWindow(TypedDict):
    dataset_id: str
    window_start: datetime
    window_end: datetime


class InvestigationState(TypedDict, total=False):
    """Every value has a precise type, so a saved step (checkpoint) can be turned back
    into exactly these objects by validation, never by unpickling arbitrary objects."""

    investigation_id: str
    question: str
    as_of: datetime | None           # None = end of the loaded data
    dataset: DatasetWindow
    plan: Plan
    transaction: TransactionFindings
    pattern: PatternFindings
    forecast: ForecastFindings
    impact: ImpactFindings
    result: dict[str, Any]           # final assembled output
    narrative: NarrativeReport | None          # what the LLM wrote (unchecked)
    narrative_note: str
    verification: VerificationReport
    verified_narrative: NarrativeReport | None # only the sentences that passed verification
    # Append-only lists, safe under parallel execution:
    trace: Annotated[list[TraceEvent], operator.add]
    tool_calls: Annotated[list[ToolCall], operator.add]
    errors: Annotated[list[AgentError], operator.add]
    llm_calls: Annotated[list[LLMCall], operator.add]
