"""Request and response shapes for the API (validated by Pydantic)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from payops.analytics.models import Figure


class Period(BaseModel):
    start: datetime
    end: datetime


class KpiResponse(BaseModel):
    current: Period
    previous: Period
    figures: list[Figure]


class TableResponse(BaseModel):
    """A generic table: list of rows plus the period it covers."""

    current: Period
    previous: Period | None = None
    rows: list[dict[str, Any]]


class InvestigationRequest(BaseModel):
    question: str = Field(min_length=5, max_length=2000)
    as_of: datetime | None = Field(default=None, description="Review the last complete week before this time (UTC)")

    @field_validator("question")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = " ".join(value.split())  # collapse whitespace and newlines
        if len(cleaned) < 5:
            raise ValueError("question is too short")
        return cleaned

    @field_validator("as_of")
    @classmethod
    def _timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("as_of must include a timezone (use UTC)")
        return value


class InvestigationResponse(BaseModel):
    investigation_id: str
    mode: Literal["deterministic"]
    question: str
    note: str
    started_at: datetime
    duration_ms: int
    report: dict[str, Any]
    text: str
