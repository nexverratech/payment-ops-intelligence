"""Typed inputs and outputs for the analytics layer.

Every output number is wrapped in a `Figure` that says what KIND of number it is:

* measured     - counted directly from the data (e.g. failed payments last week)
* estimate     - computed from the data using a stated method that involves a
                 choice, such as which baseline to compare against
* hypothetical - depends on an assumption YOU supply (e.g. cost per failure)

The report and the agents must keep these three apart.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Kind = Literal["measured", "estimate", "hypothetical"]

CODE_PATTERN = r"^[A-Z_]{1,32}$"

# Allowed values, mirroring the CHECK constraints in db/schema.sql (a test keeps them in sync).
PAYMENT_METHODS = ("CARD", "BANK_TRANSFER", "WALLET", "NET_BANKING")
FAILURE_REASONS = (
    "INSUFFICIENT_FUNDS", "DO_NOT_HONOR", "AUTHENTICATION_FAILED",
    "FRAUD_SUSPECTED", "INVALID_DETAILS", "TIMEOUT", "PROVIDER_ERROR",
)
MAX_SPAN = timedelta(days=400)


class Figure(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    value: float
    unit: str            # "payments", "USD", "%", "percentage points", ...
    kind: Kind
    method: str          # one sentence: how it was calculated


class AnalysisFilter(BaseModel):
    """Which payments to analyse. `end` is exclusive: [start, end)."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    provider_codes: tuple[str, ...] = ()
    payment_methods: tuple[str, ...] = ()
    # Only count failures with these reasons (attempts are still all payments).
    failure_reasons: tuple[str, ...] = ()

    @field_validator("start", "end")
    @classmethod
    def _must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("datetimes must include a timezone (use UTC)")
        return value

    @field_validator("provider_codes", "payment_methods", "failure_reasons")
    @classmethod
    def _codes_look_like_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        import re

        for v in values:
            if not re.fullmatch(CODE_PATTERN, v):
                raise ValueError(f"'{v}' is not a valid code (UPPER_CASE letters and underscores only)")
        return tuple(sorted(set(values)))

    @model_validator(mode="after")
    def _sensible_range(self) -> AnalysisFilter:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.end - self.start > MAX_SPAN:
            raise ValueError("date range is longer than 400 days")
        return self

    def shifted(self, delta: timedelta) -> AnalysisFilter:
        """Same filter, moved in time (e.g. one week earlier for a baseline)."""
        return self.model_copy(update={"start": self.start + delta, "end": self.end + delta})


class AnomalyWindow(BaseModel):
    """A stretch of time in which a segment failed far more often than its own recent history."""

    segment: str                 # e.g. provider code, or "ALL"
    start: datetime
    end: datetime                # exclusive
    attempts: int
    failures: int
    expected_failures: float
    excess_failures: float
    baseline_rate: float
    window_rate: float
    peak_hour_rate: float
    max_z: float
    flagged_hours: int


class RelatedEvent(BaseModel):
    kind: Literal["deployment", "provider_notice"]
    event_id: str
    at: datetime
    hours_from_anomaly_start: float   # negative = before the anomaly began
    same_provider: bool | None = None
    description: str
    relationship: str                 # plain-English, never claims cause


class ForecastPoint(BaseModel):
    date: str
    value: float
    lower: float
    upper: float


class ForecastResult(BaseModel):
    target: Literal["attempts", "failure_rate"]
    status: Literal["ok", "insufficient_data"]
    method: str
    horizon_days: int
    points: list[ForecastPoint] = Field(default_factory=list)
    backtest: dict[str, float] | None = None   # MAE of model vs baseline on held-out days
    model_beats_baseline: bool | None = None
    limitations: list[str] = Field(default_factory=list)
