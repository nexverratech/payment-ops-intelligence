"""Business impact of an anomaly, with measured, estimated and hypothetical
figures kept strictly apart.

Important: "affected transaction value" is the total amount of payments that
failed. It is NOT revenue and NOT a loss: many customers retry and succeed,
and a payment company earns only a small fee on each payment anyway.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from payops.analytics.models import Figure


class ImpactAssumptions(BaseModel):
    """Inputs that are NOT in the data. Change them to explore scenarios."""

    currency: str = "USD"
    # Illustrative internal cost of handling one extra failed payment
    # (support contact, retry processing, merchant follow-up). Not a measured number.
    cost_per_failure_scenarios: dict[str, float] = Field(
        default_factory=lambda: {"low": 0.25, "base": 1.00, "high": 3.00}
    )
    projection_hours: float = Field(default=24, gt=0, le=24 * 30)


def window_impact(
    *,
    attempts: int,
    failures: int,
    failed_value: float,
    expected_failures: float,
    duration_hours: float,
    assumptions: ImpactAssumptions | None = None,
) -> list[Figure]:
    """Impact figures for one anomaly window (or any period with a baseline)."""
    a = assumptions or ImpactAssumptions()
    if duration_hours <= 0:
        raise ValueError("duration_hours must be positive")
    if not 0 <= failures <= attempts:
        raise ValueError("need 0 <= failures <= attempts")

    excess = failures - expected_failures
    excess_pos = max(excess, 0.0)
    avg_failed_amount = failed_value / failures if failures else 0.0
    per_hour = excess_pos / duration_hours

    figures = [
        Figure(name="failed_payments", value=failures, unit="payments", kind="measured",
               method="Count of payments with status FAILED in the window."),
        Figure(name="affected_transaction_value", value=round(failed_value, 2), unit=a.currency, kind="measured",
               method="Sum of the amounts of failed payments. Transaction value, not revenue or loss."),
        Figure(name="expected_failures", value=round(expected_failures, 1), unit="payments", kind="estimate",
               method="Attempts in the window x the baseline failure rate from before the window."),
        Figure(name="excess_failures", value=round(excess, 1), unit="payments", kind="estimate",
               method="Failed payments minus expected failures."),
        Figure(name="excess_affected_value", value=round(excess_pos * avg_failed_amount, 2), unit=a.currency, kind="estimate",
               method="Excess failures x average amount of failed payments in the window."),
        Figure(name="excess_failures_per_hour", value=round(per_hour, 2), unit="payments/hour", kind="estimate",
               method="Excess failures / window duration in hours."),
        Figure(name="projected_additional_failures", value=round(per_hour * a.projection_hours, 1), unit="payments",
               kind="hypothetical",
               method=f"IF the excess rate continued unchanged for {a.projection_hours:g} more hours. Not a forecast."),
    ]
    for scenario, cost in sorted(a.cost_per_failure_scenarios.items(), key=lambda kv: kv[1]):
        figures.append(Figure(
            name=f"handling_cost_{scenario}", value=round(excess_pos * cost, 2), unit=a.currency, kind="hypothetical",
            method=f"Excess failures x assumed {cost:.2f} {a.currency} handling cost per failure ({scenario} scenario).",
        ))
    return figures
