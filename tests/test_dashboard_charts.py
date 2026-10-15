"""Chart builders: right traces, fixed provider colours, event markers."""

import pytest

from payops.dashboard import charts

ROWS = [
    {"provider_code": p, "bucket": f"2026-09-2{d}T00:00:00Z", "attempts": 100, "failures": f, "failure_rate": f / 100}
    for p, f in (("KESTREL", 20), ("NOVAPAY", 3), ("ORBIT", 4)) for d in (1, 2)
]
EVENTS = [
    {"kind": "deployment", "deployment_id": "DEP-1", "deployed_at": "2026-09-21T12:00:00Z", "service": "s", "version": "v1", "change_summary": "c"},
    {"kind": "provider_notice", "incident_id": "INC-3", "provider_code": "KESTREL", "reported_start_at": "2026-09-21T17:00:00Z",
     "reported_end_at": "2026-09-22T04:00:00Z", "title": "degraded"},
]


def test_one_line_per_provider_with_its_fixed_colour():
    fig = charts.failure_rate_trend(ROWS)
    lines = {t.name: t.line.color for t in fig.data}
    assert lines == {"KESTREL": "#eb6834", "NOVAPAY": "#2a78d6", "ORBIT": "#eda100"}
    assert list(fig.data[0].y) == [20.0, 20.0]   # shown as percent


def test_colour_does_not_change_when_other_providers_are_filtered_out():
    only_orbit = [r for r in ROWS if r["provider_code"] == "ORBIT"]
    assert charts.failure_rate_trend(only_orbit).data[0].line.color == charts.PROVIDER_COLORS["ORBIT"]


def test_events_are_drawn_as_line_and_shaded_span():
    fig = charts.failure_rate_trend(ROWS, EVENTS)
    kinds = sorted(s.type for s in fig.layout.shapes)
    assert kinds == ["line", "rect"]
    assert {a.text for a in fig.layout.annotations} >= {"DEP-1", "INC-3"}


def test_single_series_is_labelled_all_providers():
    rows = [{k: v for k, v in r.items() if k != "provider_code"} for r in ROWS[:2]]
    assert charts.failure_rate_trend(rows).data[0].name == "All providers"


def test_excess_bar_uses_provider_colours():
    rows = [{"provider_code": "KESTREL", "excess_failures": 30, "rate_baseline": 0.02, "rate_current": 0.08, "contribution_share": 1.0},
            {"provider_code": "NOVAPAY", "excess_failures": -5, "rate_baseline": 0.03, "rate_current": 0.02, "contribution_share": 0}]
    bar = charts.provider_excess_bar(rows).data[0]
    assert list(bar.y) == ["NOVAPAY", "KESTREL"]               # sorted, largest at the top
    assert list(bar.marker.color) == ["#2a78d6", "#eb6834"]
    assert list(bar.text) == ["-5", "+30"]


def test_reasons_bar_compares_two_periods():
    rows = [{"failure_reason": "TIMEOUT", "failures": 35, "failures_previous": 5},
            {"failure_reason": "INSUFFICIENT_FUNDS", "failures": 15, "failures_previous": 15}]
    fig = charts.reasons_bar(rows)
    assert [t.name for t in fig.data] == ["Previous period", "Selected period"]
    assert list(fig.data[1].x) == [15, 35]


def test_unknown_provider_gets_neutral_colour_not_a_new_hue():
    assert charts.provider_color("NEWPAY") == charts.OTHER_COLOR


def test_volume_trend_sums_providers():
    fig = charts.volume_trend(ROWS)
    assert list(fig.data[0].y) == [300, 300]


def test_points_built_on_too_few_payments_are_left_out():
    rows = [{"provider_code": "ORBIT", "bucket": "2026-09-21T02:00:00Z", "attempts": 12, "failures": 2, "failure_rate": 2 / 12},
            {"provider_code": "ORBIT", "bucket": "2026-09-21T12:00:00Z", "attempts": 90, "failures": 3, "failure_rate": 3 / 90}]
    y = list(charts.failure_rate_trend(rows).data[0].y)
    assert y[0] is None and y[1] == pytest.approx(100 * 3 / 90)


def test_close_deployment_labels_are_staggered():
    events = [{"kind": "deployment", "deployment_id": f"DEP-{i}", "deployed_at": f"2026-09-27T1{i}:00:00Z"} for i in range(3)]
    shifts = [a.yshift for a in charts.failure_rate_trend(ROWS, events).layout.annotations]
    assert len(set(shifts)) == 3
