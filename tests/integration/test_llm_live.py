"""The real local LLM (Ollama) on the real data. Skipped unless Ollama is running with the configured model.

This test does not expect particular wording (LLM output varies); it checks that
whatever the model writes is either verified or replaced by the labelled template.
"""

import pytest
from sqlalchemy import text

from payops import health
from payops.agents.graph import run_investigation
from payops.agents.schemas import Sentence
from payops.agents.verification_agent import _sources, check_sentence
from payops.config import get_settings
from payops.db.connection import get_engine, get_readonly_engine
from payops.llm.client import OllamaClient

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def settings():
    s = get_settings()
    if health.check_database(s).status != "ok":
        pytest.skip("PostgreSQL is not reachable")
    with get_engine(s).connect() as c:
        if c.execute(text("SELECT to_regclass('payments.dataset_runs')")).scalar() is None:
            pytest.skip("No dataset loaded")
    if health.check_llm(s).status != "ok":
        pytest.skip(f"Ollama with model {s.ollama_model} is not available")
    return s


def test_live_llm_investigation_is_verified_or_falls_back(settings):
    llm = OllamaClient.from_settings(settings)
    r = run_investigation(get_readonly_engine(settings), "What went wrong with Kestrel in the week of 21 September 2026?", llm=llm)
    print("\n" + r["report_text"])   # visible with: pytest -s tests/integration/test_llm_live.py
    assert r["status"] == "complete"
    assert r["mode"] in {"llm", "template"} and r["mode_reason"]
    assert len(r["llm_calls"]) <= settings.llm_max_calls
    plan = r["plan"]
    if plan["planner"] == "llm":                          # if the model's plan was accepted, it must be valid
        assert set(plan["provider_codes"]) <= {"KESTREL", "NOVAPAY", "BLUERIVER", "ORBIT"}
    if r["mode"] == "llm":                                # every sentence shown must pass the checks again
        sources = _sources(r)
        n = r["narrative"]
        for s in n["summary"] + [x for sec in n["sections"] for x in sec["sentences"]] + n["next_steps"]:
            assert check_sentence(Sentence(**s), sources) == [], s
