"""Every number that shapes the synthetic world, in one place.

Changing a value here (or passing a different seed) produces a different but
fully reproducible dataset. Nothing in this file is random by itself.

All provider names, customers and amounts are fictional.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date


@dataclass(frozen=True)
class ProviderProfile:
    code: str                 # short ID used in the database, e.g. "KESTREL"
    display_name: str         # e.g. "Kestrel Gateway"
    traffic_share: float      # fraction of all payments routed to this provider
    base_failure_rate: float  # normal probability that a payment fails
    median_latency_ms: float  # typical processing time for a successful payment


@dataclass(frozen=True)
class InjectedIncident:
    """A problem we deliberately put into the data, so we can later check
    whether the system finds it. Positions are relative to the END of the
    data window, so the incident stays in "last week" whatever `days` is."""

    key: str                       # name used in ground_truth.json
    provider_code: str
    failure_reason: str            # the reason code the extra failures get
    start_days_before_end: int     # which day the problem starts (0 = last day)
    start_hour: int
    duration_hours: float
    peak_extra_failure_rate: float  # added on top of the normal failure rate
    latency_multiplier_at_peak: float = 1.0  # slow-down of successful payments
    # The provider's public status notice (often posted AFTER the problem starts):
    reported_delay_hours: float | None = None
    reported_duration_hours: float | None = None
    reported_title: str = ""
    reported_severity: str = "major"


@dataclass(frozen=True)
class GenerationConfig:
    seed: int = 42
    start_date: date = date(2026, 7, 1)
    days: int = 90
    base_daily_volume: int = 5_000      # average payment attempts per day at the start
    daily_growth: float = 0.002         # +0.2% volume per day (business is growing)
    customers: int = 60_000
    currency: str = "USD"

    providers: tuple[ProviderProfile, ...] = (
        ProviderProfile("NOVAPAY", "NovaPay", 0.35, 0.025, 320),
        ProviderProfile("KESTREL", "Kestrel Gateway", 0.25, 0.030, 380),
        ProviderProfile("BLUERIVER", "BlueRiver Payments", 0.25, 0.028, 350),
        ProviderProfile("ORBIT", "Orbit Acquiring", 0.15, 0.035, 420),
    )

    # Payment method -> (share of payments, amount multiplier)
    payment_methods: tuple[tuple[str, float, float], ...] = (
        ("CARD", 0.45, 1.0),
        ("BANK_TRANSFER", 0.25, 2.5),
        ("WALLET", 0.20, 0.6),
        ("NET_BANKING", 0.10, 1.8),
    )

    # Why ordinary (non-incident) payments fail, and how often each reason occurs.
    failure_reasons: tuple[tuple[str, float], ...] = (
        ("INSUFFICIENT_FUNDS", 0.35),
        ("DO_NOT_HONOR", 0.20),
        ("AUTHENTICATION_FAILED", 0.12),
        ("FRAUD_SUSPECTED", 0.10),
        ("INVALID_DETAILS", 0.08),
        ("TIMEOUT", 0.08),
        ("PROVIDER_ERROR", 0.07),
    )

    # Relative traffic for each hour of the day (UTC). Quiet at night, busy in the evening.
    hourly_weights: tuple[float, ...] = (
        0.30, 0.22, 0.18, 0.16, 0.18, 0.28, 0.45, 0.70, 0.90, 1.00, 1.05, 1.10,
        1.15, 1.10, 1.05, 1.05, 1.10, 1.20, 1.30, 1.35, 1.25, 1.00, 0.70, 0.45,
    )
    # Monday=0 ... Sunday=6
    weekday_multipliers: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0, 1.05, 1.10, 0.90)

    median_amount: float = 45.0
    amount_spread: float = 0.9          # log-normal sigma: a few large payments, many small ones
    daily_failure_noise: float = 0.08   # day-to-day wobble in each provider's failure rate
    timeout_latency_ms: int = 30_000    # a timed-out payment waited this long

    incidents: tuple[InjectedIncident, ...] = (
        # The main scenario: Kestrel times out for ~36 hours in the last week.
        InjectedIncident(
            key="primary",
            provider_code="KESTREL",
            failure_reason="TIMEOUT",
            start_days_before_end=5,
            start_hour=14,
            duration_hours=36,
            peak_extra_failure_rate=0.20,
            latency_multiplier_at_peak=3.0,
            reported_delay_hours=3,
            reported_duration_hours=35.5,
            reported_title="Kestrel Gateway: degraded performance on authorisation API",
            reported_severity="major",
        ),
        # A shorter, smaller problem earlier on, so detection can be scored on more than one event.
        InjectedIncident(
            key="secondary",
            provider_code="ORBIT",
            failure_reason="PROVIDER_ERROR",
            start_days_before_end=31,
            start_hour=10,
            duration_hours=3,
            peak_extra_failure_rate=0.25,
            reported_delay_hours=0.5,
            reported_duration_hours=2.5,
            reported_title="Orbit Acquiring: intermittent processing errors",
            reported_severity="minor",
        ),
    )

    def to_dict(self) -> dict:
        """Plain dict (dates as text) for saving next to the data."""
        d = asdict(self)
        d["start_date"] = self.start_date.isoformat()
        return d
