"""Each agent on its own, with the database replaced by canned data (tests/fakes.py).
No LangGraph needed: agents are plain functions (state, tools) -> update."""

from datetime import datetime, timedelta, timezone

import pytest

import fakes
from payops.agents import coordinator, forecast_agent, impact_agent, pattern_agent, transaction_agent
from payops.agents.pattern_agent import rank_hypotheses
from payops.agents.schemas import AnomalySummary
from payops.agents.tools import TOOL_PERMISSIONS, AgentTools, ToolPermissionError
from payops.analytics.events import change_around
from payops.analytics.models import AnalysisFilter
from payops.analytics.events import RelatedEvent

UTC = timezone.utc
WEEK = AnalysisFilter(start=fakes.LAST_WEEK[0], end=fakes.LAST_WEEK[1])


def planned(monkeypatch, **state):
    fakes.install(monkeypatch)
    s = {"question": "Why did failures rise?", "as_of": None, **state}
    s.update(coordinator.plan(s, AgentTools("coordinator", None)))
    return s


# ---- tools: permissions and recording ---------------------------------------------

def test_agent_cannot_use_a_tool_outside_its_permissions(monkeypatch):
    fakes.install(monkeypatch)
    tools = AgentTools("forecast", None)
    with pytest.raises(ToolPermissionError):
        tools.summary(WEEK)
    assert tools.calls[-1].status == "denied" and tools.calls[-1].tool == "summary"


def test_unknown_agent_gets_no_tools():
    with pytest.raises(ToolPermissionError):
        AgentTools("intruder", None)


def test_every_tool_call_is_recorded_with_its_inputs(monkeypatch):
    fakes.install(monkeypatch)
    tools = AgentTools("transaction", None)
    tools.breakdown(WEEK.model_copy(update={"provider_codes": ("KESTREL",)}), "provider")
    call = tools.calls[0]
    assert call.call_id == "transaction-1" and call.status == "ok" and call.rows == 2
    assert call.params["providers"] == ["KESTREL"] and call.params["by"] == "provider"


def test_coordinator_may_only_read_the_dataset_window_and_provider_list():
    assert TOOL_PERMISSIONS["coordinator"] == frozenset({"dataset_window", "providers"})


# ---- coordinator: plan -----------------------------------------------------------------

def test_plan_defaults_to_last_complete_week(monkeypatch):
    plan = planned(monkeypatch)["plan"]
    assert (plan.current_start, plan.current_end) == fakes.LAST_WEEK
    assert plan.baseline_start == fakes.LAST_WEEK[0] - timedelta(days=7)
    assert plan.steps == ["transaction", "pattern", "forecast", "impact"]


def test_plan_clamps_as_of_to_the_end_of_the_data(monkeypatch):
    plan = planned(monkeypatch, as_of=datetime(2027, 1, 1, tzinfo=UTC))["plan"]
    assert plan.as_of == fakes.WINDOW["window_end"]


def test_plan_refuses_a_week_without_history(monkeypatch):
    fakes.install(monkeypatch)
    with pytest.raises(ValueError, match="Not enough data"):
        coordinator.plan({"question": "q", "as_of": datetime(2026, 7, 9, tzinfo=UTC)}, AgentTools("coordinator", None))


# ---- transaction agent ------------------------------------------------------------------

def test_transaction_agent_finds_the_contributor_and_reason(monkeypatch):
    state = planned(monkeypatch)
    tools = AgentTools("transaction", None)
    out = transaction_agent.run(state, tools)["transaction"]
    assert out.top_provider == "KESTREL" and out.top_provider_share == pytest.approx(1.0)
    f = {x.finding_id: x for x in out.findings}
    assert "5.00%" in f["TX-1"].statement and "2.00%" in f["TX-1"].statement
    assert "TIMEOUT failures went from 5 to 35" in f["TX-3"].statement
    assert f["TX-3"].evidence.sample_transaction_ids == ["TXN-00000001", "TXN-00000002", "TXN-00000003"]
    made = {c.call_id for c in tools.calls}
    for finding in out.findings:   # every cited call really happened
        assert set(finding.evidence.tool_call_ids) <= made and finding.evidence.tool_call_ids


# ---- pattern agent and hypothesis ranking ------------------------------------------------

def test_pattern_agent_ranks_provider_side_first(monkeypatch):
    state = planned(monkeypatch)
    out = pattern_agent.run(state, AgentTools("pattern", None))["pattern"]
    [a] = out.anomalies
    assert a.segment == "KESTREL" and a.dominant_reason == "TIMEOUT" and a.dominant_reason_share == pytest.approx(0.7)
    first, second = out.hypotheses
    assert first.hypothesis_id == "H-PROVIDER" and first.score == 6 and first.status == "leading (unconfirmed)"
    assert second.hypothesis_id == "H-DEP-1" and second.status == "unlikely"
    assert any("not confirmed" in f.statement for f in out.findings if f.kind == "hypothesis")


def _anomaly(segment="KESTREL", reason="TIMEOUT", start=datetime(2026, 9, 23, 14, tzinfo=UTC)):
    return AnomalySummary(segment=segment, start=start, end=start + timedelta(hours=10), attempts=1000, failures=200,
                          expected_failures=30, excess_failures=170, baseline_rate=0.03, window_rate=0.2, max_z=10,
                          dominant_reason=reason, dominant_reason_share=0.8)


def _deployment(at):
    return RelatedEvent(kind="deployment", event_id="DEP-9", at=at, hours_from_anomaly_start=-1.0,
                        description="checkout-service v3", relationship="x")


def _risers_table(rising):
    import pandas as pd
    return pd.DataFrame([{"provider_code": p, "significant_increase": p in rising} for p in ("KESTREL", "NOVAPAY", "ORBIT", "ALL")])


def test_deployment_leads_when_every_provider_got_worse_after_it():
    main = _anomaly()
    others = [_anomaly("NOVAPAY"), _anomaly("ORBIT")]
    dep = _deployment(main.start - timedelta(hours=1))
    ranked = rank_hypotheses(main, [main, *others], [dep], {"DEP-9": _risers_table({"KESTREL", "NOVAPAY", "ORBIT"})}, [])
    assert ranked[0].hypothesis_id == "H-DEP-9" and ranked[0].score == 3
    provider = next(h for h in ranked if h.hypothesis_id == "H-PROVIDER")
    assert any("Other providers also show anomalies" in c for c in provider.contradicting)


def test_customer_side_hypothesis_appears_for_customer_declines():
    ranked = rank_hypotheses(_anomaly(reason="INSUFFICIENT_FUNDS"), [_anomaly(reason="INSUFFICIENT_FUNDS")], [], {}, [])
    assert "H-CUSTOMER" in {h.hypothesis_id for h in ranked}


def test_no_anomaly_gives_an_honest_empty_result(monkeypatch):
    state = planned(monkeypatch)
    monkeypatch.setattr(fakes, "LAST_WEEK", fakes.LAST_WEEK)  # keep reference stable
    from payops.analytics import queries
    monkeypatch.setattr(queries, "time_series", lambda conn, f, unit, by: fakes.hourly(f.start, days=16))  # no spike
    out = pattern_agent.run(state, AgentTools("pattern", None))["pattern"]
    assert out.anomalies == [] and out.hypotheses == []
    assert "No anomaly window was detected" in out.findings[0].statement


# ---- forecast and impact agents ----------------------------------------------------------

def test_forecast_agent_reports_backtest(monkeypatch):
    out = forecast_agent.run(planned(monkeypatch), AgentTools("forecast", None))["forecast"]
    assert out.volume["status"] == "ok"
    assert "naive" in out.findings[0].statement and len(out.findings) == 2


def test_forecast_agent_refuses_with_too_little_history(monkeypatch):
    state = planned(monkeypatch)
    state["dataset"] = {**state["dataset"], "window_start": fakes.LAST_WEEK[0] - timedelta(days=10)}
    out = forecast_agent.run(state, AgentTools("forecast", None))["forecast"]
    assert out.volume["status"] == "insufficient_data"
    assert out.findings[0].statement.startswith("No forecast")


def test_impact_uses_the_anomaly_window_and_labels_every_statement(monkeypatch):
    state = planned(monkeypatch)
    state.update(pattern_agent.run(state, AgentTools("pattern", None)))
    out = impact_agent.run(state, AgentTools("impact", None))["impact"]
    assert out.basis == "anomaly_window"
    starts = [f.statement.split("]")[0] + "]" for f in out.findings]
    assert starts == ["[measured]", "[estimate]", "[hypothetical]"]
    assert "not revenue or loss" in out.findings[0].statement


def test_impact_falls_back_to_period_comparison_without_pattern(monkeypatch):
    state = planned(monkeypatch)
    state.update(transaction_agent.run(state, AgentTools("transaction", None)))
    out = impact_agent.run(state, AgentTools("impact", None))["impact"]
    assert out.basis == "period_comparison"
    # 50 failures, expected 1000 x 2% = 20 -> 30 excess
    assert next(f for f in out.figures if f.name == "excess_failures").value == 30


def test_change_around_used_by_pattern_handles_single_provider():
    h = fakes.hourly(datetime(2026, 9, 12, tzinfo=UTC), days=16, spike_at=datetime(2026, 9, 23, 14, tzinfo=UTC))
    table = change_around(h, datetime(2026, 9, 23, 12, tzinfo=UTC), 24)
    assert set(table["provider_code"]) == {"KESTREL", "ALL"}
