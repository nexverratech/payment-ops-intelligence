"""Structured inputs and outputs of every agent.

Agents talk to each other only through these typed objects, never through
free text. That is what makes the workflow testable and the evidence checkable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from payops.analytics.models import Figure

AgentName = Literal["coordinator", "transaction", "pattern", "forecast", "impact", "verification"]


class ToolCall(BaseModel):
    """One call an agent made to an analytics tool: the audit trail behind every number."""

    call_id: str                     # e.g. "transaction-3"
    agent: str
    tool: str                        # e.g. "summary"
    params: dict[str, Any]           # JSON-safe description of the inputs
    rows: int | None = None          # size of the result, where meaningful
    duration_ms: int
    status: Literal["ok", "denied", "failed"]
    error: str | None = None


class Evidence(BaseModel):
    tool_call_ids: list[str] = Field(default_factory=list)
    sample_transaction_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    finding_id: str                  # e.g. "TX-2"
    agent: str
    kind: Literal["observation", "hypothesis", "forecast", "impact"]
    statement: str                   # plain English, built from the figures by code
    figures: list[Figure] = Field(default_factory=list)
    evidence: Evidence = Field(default_factory=Evidence)


class Hypothesis(BaseModel):
    """A possible explanation, ranked by transparent evidence points. Never a proven cause."""

    hypothesis_id: str
    title: str
    rank: int
    score: int
    status: Literal["leading (unconfirmed)", "alternative", "unlikely"]
    supporting: list[str]
    contradicting: list[str]
    next_checks: list[str]
    evidence: Evidence = Field(default_factory=Evidence)


class Plan(BaseModel):
    question: str
    as_of: datetime
    current_start: datetime
    current_end: datetime
    baseline_start: datetime
    baseline_end: datetime
    provider_codes: tuple[str, ...] = ()
    steps: list[str]
    interpretation: str
    planner: Literal["deterministic", "llm"] = "deterministic"


class TraceEvent(BaseModel):
    agent: str
    status: Literal["ok", "failed", "skipped"]
    started_at: datetime
    duration_ms: int
    tool_call_ids: list[str] = Field(default_factory=list)
    message: str = ""


class AgentError(BaseModel):
    agent: str
    error_type: str
    message: str


class TransactionFindings(BaseModel):
    overall: list[Figure]
    by_provider: list[dict[str, Any]]
    by_method: list[dict[str, Any]]
    top_provider: str | None
    top_provider_share: float | None
    top_provider_reasons: list[dict[str, Any]]
    findings: list[Finding]


class AnomalySummary(BaseModel):
    segment: str
    start: datetime
    end: datetime
    attempts: int
    failures: int
    expected_failures: float
    excess_failures: float
    baseline_rate: float
    window_rate: float
    max_z: float
    dominant_reason: str | None
    dominant_reason_share: float | None


class PatternFindings(BaseModel):
    anomalies: list[AnomalySummary]
    related_events: list[dict[str, Any]]
    hypotheses: list[Hypothesis]
    findings: list[Finding]


class ForecastFindings(BaseModel):
    volume: dict[str, Any]
    failure_rate: dict[str, Any]
    findings: list[Finding]


class ImpactFindings(BaseModel):
    basis: Literal["anomaly_window", "period_comparison"]
    figures: list[Figure]
    assumptions: dict[str, Any]
    findings: list[Finding]


# ---- What the LLM is allowed to return (Phase 5b) ------------------------------------------
# The model must reply with JSON matching these shapes. Code validates every field
# before anything is used; nothing the model writes is executed.

class PlanProposal(BaseModel):
    """The LLM's reading of the question. Checked by code before it becomes a Plan."""

    week_containing: str | None = Field(
        default=None, description="Any date (YYYY-MM-DD) inside the week to investigate; null = the last complete week")
    provider_codes: list[str] = Field(default_factory=list, description="Provider codes to focus on; empty = all providers")
    reasoning: str = Field(default="", max_length=400, description="One sentence: how you read the question")


class Sentence(BaseModel):
    text: str = Field(max_length=600)
    cites: list[str] = Field(default_factory=list, description="IDs of the findings/hypotheses this sentence is based on")


class NarrativeSection(BaseModel):
    title: str = Field(max_length=80)
    sentences: list[Sentence]


class NarrativeReport(BaseModel):
    summary: list[Sentence] = Field(description="3 to 5 sentences answering the question")
    sections: list[NarrativeSection]
    next_steps: list[Sentence] = Field(default_factory=list)


class VerificationCheck(BaseModel):
    name: str
    status: Literal["pass", "fail", "skipped"]
    detail: str


class RemovedSentence(BaseModel):
    text: str
    cites: list[str] = Field(default_factory=list)
    reasons: list[str]


class VerificationReport(BaseModel):
    checks: list[VerificationCheck]
    sentences_total: int
    sentences_kept: int
    removed: list[RemovedSentence]
    duplicates_dropped: int = 0              # exact repeats, not counted in the totals
    evidence_support_rate: float | None      # kept sentences / all sentences
    unsupported_claim_rate: float | None     # removed sentences / all sentences
    decision: Literal["llm_report", "template_report"]
    reason: str
