"""Saved checkpoints come back as exactly the declared types, and nothing else."""

import json
import math
from datetime import datetime

import pytest
from pydantic import ValidationError

import fakes
from payops.agents.graph import run_investigation
from payops.agents.schemas import Plan, TraceEvent
from payops.history.codec import decode_update, encode_json, encode_update
from payops.history.store import MemoryStore


@pytest.fixture
def saved(monkeypatch):
    fakes.install(monkeypatch)
    store = MemoryStore()
    r = run_investigation(None, "Why did failures rise last week?", store=store)
    return {s.step: s.payload for s in store.steps(r["investigation_id"])}


def test_every_step_survives_a_save_and_load_unchanged(saved):
    for step, payload in saved.items():
        assert json.loads(encode_update(decode_update(payload))) == payload, step


def test_loading_restores_real_types(saved):
    plan = decode_update(saved["plan"])
    assert isinstance(plan["plan"], Plan) and isinstance(plan["dataset"]["window_start"], datetime)
    assert isinstance(plan["trace"][0], TraceEvent)


def test_unknown_fields_are_dropped():
    assert decode_update({"narrative_note": "ok", "run_this": "os.system('calc')"}) == {"narrative_note": "ok"}


def test_stored_data_cannot_choose_which_class_is_created():
    # A tampered checkpoint that tries to smuggle in another object is rejected by validation.
    with pytest.raises(ValidationError):
        decode_update({"plan": {"__class__": "subprocess.Popen", "args": ["calc.exe"]}})


def test_nan_and_infinity_become_null_for_postgres():
    text = encode_json({"rate": math.nan, "z": math.inf, "ok": 1.5})
    assert json.loads(text) == {"rate": None, "z": None, "ok": 1.5}


def test_an_llm_report_and_its_verification_survive_a_save_and_load(monkeypatch):
    from fake_llm import FakeLLM
    from test_agent_graph_llm import GOOD_REPORT, PLAN

    fakes.install(monkeypatch)
    store = MemoryStore()
    r = run_investigation(None, "Why did failures rise last week?", llm=FakeLLM(plan=PLAN, report=GOOD_REPORT), store=store)
    assert r["mode"] == "llm"
    steps = {s.step: s.payload for s in store.steps(r["investigation_id"])}
    narrate, verify = decode_update(steps["narrate"]), decode_update(steps["verify"])
    assert narrate["narrative"].summary[0].cites == ["TX-1"] and narrate["llm_calls"][0].purpose == "report"
    assert verify["verification"].decision == "llm_report"
    assert json.loads(encode_update(narrate)) == steps["narrate"]
