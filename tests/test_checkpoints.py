"""Checkpoints, resume, interruption, time limit and best-effort saving (in-memory store)."""

from datetime import datetime, timedelta, timezone

import pytest

import fakes
from fake_llm import FakeLLM
from payops.agents import coordinator, graph
from payops.agents.graph import latest_reusable, resume_investigation, reusable_steps, run_investigation
from payops.history.store import MemoryStore, NotFound, NotResumable, StepRecord
from payops.llm.client import LLMUnavailable

Q = "Why did the failure rate increase last week?"
ANALYSIS = ["plan", "transaction", "pattern", "forecast", "impact"]


@pytest.fixture
def store(monkeypatch):
    fakes.install(monkeypatch)
    return MemoryStore()


def break_step(monkeypatch, name, exc=RuntimeError("boom")):
    agent, fn, needs = graph.NODES[name]

    def boom(*args):
        raise exc

    monkeypatch.setitem(graph.NODES, name, (agent, boom, needs))
    return lambda: monkeypatch.setitem(graph.NODES, name, (agent, fn, needs))   # call to repair


def count_calls(monkeypatch, name):
    agent, fn, needs = graph.NODES[name]
    calls = []

    def counted(*args):
        calls.append(1)
        return fn(*args)

    monkeypatch.setitem(graph.NODES, name, (agent, counted, needs))
    return calls


# ---- saving ------------------------------------------------------------------------------

def test_every_step_is_saved_and_the_record_completes(store):
    r = run_investigation(None, Q, store=store)
    assert r["persisted"] is True and r["run"] == 1 and r["reused_steps"] == []
    rec = store.get(r["investigation_id"])
    assert rec.status == "complete" and rec.mode == "template" and rec.dataset_id == "test-dataset"
    assert rec.created_by is None                              # no owner given
    assert rec.result["report_text"] == r["report_text"]
    steps = store.steps(r["investigation_id"])
    assert sorted(s.step for s in steps) == sorted(graph.NODES) and all(s.status == "ok" and s.reusable for s in steps)


def test_without_a_store_nothing_is_saved_but_the_investigation_works(monkeypatch):
    fakes.install(monkeypatch)
    r = run_investigation(None, Q)
    assert r["status"] == "complete" and r["persisted"] is False


class BrokenStore(MemoryStore):
    def __init__(self, fail_on):
        super().__init__()
        self.fail_on = fail_on

    def create(self, *a, **kw):
        if self.fail_on == "create":
            raise ConnectionError("history database down")
        super().create(*a, **kw)

    def save_step(self, *a):
        if self.fail_on == "save_step":
            raise ConnectionError("history database down")
        super().save_step(*a)


@pytest.mark.parametrize("fail_on", ["create", "save_step"])
def test_a_history_outage_does_not_break_the_investigation(monkeypatch, fail_on):
    fakes.install(monkeypatch)
    r = run_investigation(None, Q, store=BrokenStore(fail_on))
    assert r["status"] == "complete" and r["report_text"] and r["persisted"] is False


# ---- resume after a failed step -------------------------------------------------------------

def test_resume_reuses_finished_steps_and_reruns_the_rest(store, monkeypatch):
    repair = break_step(monkeypatch, "pattern")
    first = run_investigation(None, Q, store=store)
    assert first["status"] == "partial" and store.get(first["investigation_id"]).status == "partial"

    repair()
    transaction_runs = count_calls(monkeypatch, "transaction")
    second = resume_investigation(None, store, first["investigation_id"])

    assert second["status"] == "complete" and second["run"] == 2
    assert second["reused_steps"] == ["forecast", "plan", "transaction"]
    assert transaction_runs == []                                   # really not recomputed
    reused = {t["agent"] for t in second["trace"] if t["from_checkpoint"]}
    assert reused == {"plan", "transaction", "forecast"}
    assert second["findings"] and second["hypotheses"][0]["status"] == "leading (unconfirmed)"
    rec = store.get(first["investigation_id"])
    assert rec.status == "complete" and rec.runs == 2


def test_resumed_result_matches_a_fresh_run(store, monkeypatch):
    fresh = run_investigation(None, Q, store=store)
    repair = break_step(monkeypatch, "impact")
    first = run_investigation(None, Q, store=store)
    repair()
    resumed = resume_investigation(None, store, first["investigation_id"])
    for key in ("status", "headline", "findings", "hypotheses", "anomalies", "uncertainties", "report_text"):
        assert resumed[key] == fresh[key], key


def test_evidence_still_traces_after_resume(store, monkeypatch):
    repair = break_step(monkeypatch, "forecast")
    first = run_investigation(None, Q, store=store)
    repair()
    r = resume_investigation(None, store, first["investigation_id"])
    ok = {c["call_id"] for c in r["tool_calls"] if c["status"] == "ok"}
    for f in r["findings"]:
        assert set(f["evidence"]["tool_call_ids"]) <= ok
    assert all(c["status"] != "fail" for c in r["verification"]["checks"])


# ---- interruption ----------------------------------------------------------------------------

class ProcessStopped(BaseException):
    """Like Ctrl+C: not an ordinary error, so the agent wrapper does not catch it."""


def test_a_stopped_process_is_recorded_as_interrupted_and_can_be_resumed(store, monkeypatch):
    repair = break_step(monkeypatch, "impact", ProcessStopped())
    with pytest.raises(BaseException):
        run_investigation(None, Q, investigation_id="00000000-0000-4000-8000-000000000001", store=store)
    rec = store.get("00000000-0000-4000-8000-000000000001")
    assert rec.status == "interrupted"

    repair()
    r = resume_investigation(None, store, rec.investigation_id)
    assert r["status"] == "complete"
    assert set(r["reused_steps"]) == {"plan", "transaction", "pattern", "forecast"}


def test_a_run_with_no_heartbeat_is_marked_interrupted(store):
    store.create("00000000-0000-4000-8000-000000000002", Q, None, False)
    store.records["00000000-0000-4000-8000-000000000002"].updated_at -= timedelta(hours=1)
    assert store.mark_stale_interrupted(600) == 1
    assert store.get("00000000-0000-4000-8000-000000000002").status == "interrupted"
    r = resume_investigation(None, store, "00000000-0000-4000-8000-000000000002")   # nothing saved: runs everything
    assert r["status"] == "complete" and r["reused_steps"] == []


def test_a_recent_running_investigation_is_left_alone(store):
    store.create("00000000-0000-4000-8000-000000000003", Q, None, False)
    assert store.mark_stale_interrupted(600) == 0


# ---- what may not be resumed --------------------------------------------------------------

def test_a_complete_investigation_cannot_be_resumed(store):
    r = run_investigation(None, Q, store=store)
    with pytest.raises(NotResumable, match="complete"):
        resume_investigation(None, store, r["investigation_id"])


def test_unknown_investigation(store):
    with pytest.raises(NotFound):
        resume_investigation(None, store, "00000000-0000-4000-8000-00000000dead")


def test_two_resumes_cannot_run_at_once(store, monkeypatch):
    break_step(monkeypatch, "pattern")
    r = run_investigation(None, Q, store=store)
    store.claim_for_resume(r["investigation_id"])          # someone else just started resuming it
    with pytest.raises(NotResumable, match="already running"):
        resume_investigation(None, store, r["investigation_id"])


def test_resume_is_refused_when_the_data_was_reloaded(store, monkeypatch):
    break_step(monkeypatch, "pattern")
    r = run_investigation(None, Q, store=store)
    from payops.analytics import queries

    monkeypatch.setattr(queries, "dataset_window", lambda conn: {**fakes.WINDOW, "dataset_id": "reloaded"})
    with pytest.raises(NotResumable, match="data was reloaded"):
        resume_investigation(None, store, r["investigation_id"])
    assert store.get(r["investigation_id"]).status == "partial"      # untouched, not stuck in "running"


# ---- the reuse rules ---------------------------------------------------------------------------

def test_a_step_is_reused_only_if_everything_it_depends_on_is():
    saved = {n: {} for n in ("plan", "transaction", "forecast", "impact", "assemble", "narrate")}   # pattern missing
    assert set(reusable_steps(saved)) == {"plan", "transaction", "forecast"}


def test_a_later_failure_makes_an_older_checkpoint_stale():
    rows = [StepRecord(step="pattern", run=1, status="ok", reusable=True, payload={"a": 1}, duration_ms=1),
            StepRecord(step="pattern", run=2, status="failed", reusable=False, payload={}, duration_ms=1),
            StepRecord(step="plan", run=1, status="ok", reusable=True, payload={"p": 1}, duration_ms=1)]
    assert latest_reusable(rows) == {"plan": {"p": 1}}


@pytest.mark.parametrize("note, reusable", [
    (f"{coordinator.LLM_FAILURE_NOTE}: connection refused", False),    # LLM was down: try again on resume
    ("LLM switched off", True),
])
def test_a_report_written_without_a_working_llm_is_retried(note, reusable):
    assert graph._reusable("narrate", "ok", {"narrative": None, "narrative_note": note}) is reusable


def test_a_failed_llm_report_is_retried_when_resuming(store, monkeypatch):
    repair = break_step(monkeypatch, "forecast")
    first = run_investigation(None, Q, llm=FakeLLM(plan={"week_containing": None, "provider_codes": []},
                                                   report=LLMUnavailable("down")), store=store)
    assert first["mode"] == "template"
    repair()
    from test_agent_graph_llm import GOOD_REPORT

    r = resume_investigation(None, store, first["investigation_id"],
                             make_llm=lambda: FakeLLM(report=GOOD_REPORT))
    assert r["mode"] == "llm" and "plan" in r["reused_steps"]


# ---- time limit ----------------------------------------------------------------------------

def test_when_time_runs_out_the_user_still_gets_a_labelled_partial_report(store, monkeypatch):
    ticks = iter([0.0, 0.0])                  # deadline set at 0, plan starts at 0; afterwards it is "late"
    monkeypatch.setattr(graph, "_now", lambda: next(ticks, 1e9))
    r = run_investigation(None, Q, store=store, timeout_seconds=60)
    assert r["status"] == "partial"
    assert {"transaction", "pattern", "forecast", "impact", "narrate"} <= set(r["skipped_steps"])
    assert any("time limit" in u for u in r["uncertainties"])
    assert r["report_text"].startswith("[Template report used")

    monkeypatch.setattr(graph, "_now", lambda: 0.0)          # a fresh budget for the resumed run
    resumed = resume_investigation(None, store, r["investigation_id"])
    assert resumed["status"] == "complete" and resumed["reused_steps"] == ["plan"]


def test_a_report_step_that_fails_makes_the_investigation_partial(store, monkeypatch):
    break_step(monkeypatch, "verify")
    r = run_investigation(None, Q, store=store)
    assert r["status"] == "partial" and r["failed_steps"] == ["verify"]
    assert store.get(r["investigation_id"]).status == "partial"


def test_started_and_finished_times_are_recorded(store):
    before = datetime.now(timezone.utc)
    r = run_investigation(None, Q, store=store)
    rec = store.get(r["investigation_id"])
    assert before <= rec.created_at <= rec.finished_at


def test_the_owner_is_recorded(store):
    r = run_investigation(None, Q, store=store, requested_by="ana")
    assert store.get(r["investigation_id"]).created_by == "ana"
    assert [x.investigation_id for x in store.list(created_by="ana")] == [r["investigation_id"]]
    assert store.list(created_by="someone-else") == []
