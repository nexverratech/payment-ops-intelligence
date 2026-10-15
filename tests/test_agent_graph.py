"""The whole LangGraph workflow with canned data: order, parallel branch, partial failure, evidence integrity."""

import pytest

import fakes
from payops.agents import graph
from payops.agents.template_report import LABEL, render, render_trace


@pytest.fixture
def result(monkeypatch):
    fakes.install(monkeypatch)
    return graph.run_investigation(None, "Why did the failure rate increase last week?")


def test_all_steps_run_and_complete(result):
    assert result["status"] == "complete"
    assert [t["agent"] for t in result["trace"]][0] == "plan"
    assert {t["agent"] for t in result["trace"]} == {"plan", "transaction", "pattern", "forecast", "impact", "assemble"}
    assert all(t["status"] == "ok" for t in result["trace"])
    order = [t["agent"] for t in result["trace"]]
    assert order.index("impact") > max(order.index(a) for a in ("transaction", "pattern", "forecast"))


def test_every_cited_tool_call_exists_and_succeeded(result):
    ok_calls = {c["call_id"] for c in result["tool_calls"] if c["status"] == "ok"}
    for f in result["findings"]:
        assert f["evidence"]["tool_call_ids"], f"{f['finding_id']} cites no evidence"
        assert set(f["evidence"]["tool_call_ids"]) <= ok_calls, f["finding_id"]


def test_headline_and_hypotheses(result):
    assert "KESTREL" in result["headline"] and "unconfirmed" in result["headline"]
    assert result["hypotheses"][0]["status"] == "leading (unconfirmed)"


def test_a_crashing_agent_gives_a_partial_result_not_a_crash(monkeypatch):
    fakes.install(monkeypatch)

    def boom(state, tools):
        raise RuntimeError("SELECT secret FROM somewhere -- internal detail")

    agent, _, needs = graph.NODES["pattern"]
    monkeypatch.setitem(graph.NODES, "pattern", (agent, boom, needs))
    r = graph.run_investigation(None, "Why did failures rise?")
    assert r["status"] == "partial" and r["failed_steps"] == ["pattern"]
    assert r["impact_basis"] == "period_comparison"          # impact fell back
    assert "SELECT" not in str(r["errors"])                    # no internals leak
    assert any("pattern step failed" in u for u in r["uncertainties"])


def test_a_failed_plan_skips_everything_downstream(monkeypatch):
    fakes.install(monkeypatch)

    def boom(state, tools):
        raise ValueError("Not enough data before the requested week")

    agent, _, needs = graph.NODES["plan"]
    monkeypatch.setitem(graph.NODES, "plan", (agent, boom, needs))
    r = graph.run_investigation(None, "Why did failures rise?")
    assert r["failed_steps"] == ["plan"]
    assert set(r["skipped_steps"]) == {"transaction", "pattern", "forecast", "impact"}
    assert r["errors"][0]["message"].startswith("Not enough data")   # our own message is kept


def test_step_limit_is_set():
    assert graph.MAX_STEPS < 50


def test_template_report_is_labelled_as_no_llm(result):
    text = render(result)
    assert text.startswith(LABEL) and "no LLM" in text
    assert "HYPOTHESES (ranked by evidence, none proven)" in text
    assert "plan" in render_trace(result)
