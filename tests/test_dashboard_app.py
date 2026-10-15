"""Runs the real Streamlit script headlessly (Streamlit's AppTest) with the API replaced by canned answers."""

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from payops.dashboard.api_client import ApiClient, ApiError

APP = str(Path(__file__).resolve().parents[1] / "src" / "payops" / "dashboard" / "app.py")

ROWS = [{"provider_code": "KESTREL", "bucket": "2026-09-21T00:00:00Z", "attempts": 100, "failures": 7,
         "failure_rate": 0.07, "failed_value": 300.0}]
CANNED = {
    "meta": {"dataset_id": "test", "window_start": "2026-07-01T00:00:00Z", "window_end": "2026-09-29T00:00:00Z",
             "providers": [{"provider_code": "KESTREL", "display_name": "Kestrel Gateway"}],
             "payment_methods": ["CARD"], "failure_reasons": ["TIMEOUT"]},
    "health": {"status": "degraded", "version": "0.1.0", "llm_mode": "fallback",
               "components": {"database": {"status": "ok"}, "llm": {"status": "unreachable"}}},
    "kpis": {"figures": [{"name": n, "value": v} for n, v in [
        ("attempts", 1000), ("succeeded", 950), ("failures", 50), ("affected_transaction_value", 2500.0),
        ("previous_failures", 20), ("failure_rate", 5.0), ("previous_failure_rate", 2.0)]]},
    "timeseries": {"rows": ROWS},
    "events": {"rows": []},
    "breakdown": {"rows": [{"provider_code": "KESTREL", "attempts_current": 500, "failures_current": 40, "rate_baseline": 0.02,
                            "rate_current": 0.08, "change_pct_points": 6.0, "excess_failures": 30.0,
                            "contribution_share": 1.0, "p_value": 0.0001, "significant": True}]},
    "failure_reasons": {"rows": [{"failure_reason": "TIMEOUT", "failures": 35, "failures_previous": 5, "change": 30}]},
    "anomalies": {"rows": []},
    "login": {"token": "tok-123", "token_type": "bearer", "expires_at": "2026-10-15T20:00:00Z", "username": "ana",
              "role": "analyst", "permissions": ["analytics:read", "investigations:read_own", "investigations:resume_own",
                                                 "investigations:run"]},
    "audit": [{"id": 1, "at": "2026-10-15T10:00:00.000001Z", "actor": "ana", "role": "analyst", "action": "auth.login",
               "target": None, "outcome": "success", "source": "api", "details": {}, "prev_hash": "0" * 64,
               "hash": "a" * 64}],
    "history": {"limit": 50, "offset": 0, "items": [
        {"investigation_id": "7f1c2a9e-1111-4b2c-9d3e-000000000001", "question": "Why did failures rise last week?",
         "status": "interrupted", "mode": None, "headline": None, "use_llm": True, "runs": 1, "dataset_id": "test",
         "created_at": "2026-10-15T10:00:00Z", "finished_at": None, "duration_ms": None, "resumable": True}]},
}


@pytest.fixture(autouse=True)
def clear_caches():
    st.cache_data.clear()
    st.cache_resource.clear()
    yield


def canned(monkeypatch, broken=False):
    for name, value in CANNED.items():
        def method(self, *args, _value=value, **kwargs):
            if broken:
                raise ApiError("Cannot reach the API at http://127.0.0.1:8000.")
            return _value
        monkeypatch.setattr(ApiClient, name, method)


PERMS = {
    "viewer": ["analytics:read"],
    "analyst": CANNED["login"]["permissions"],
    "admin": ["analytics:read", "audit:read", "investigations:read_all", "users:manage"],
}


def signed_in(role="analyst"):
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["token"] = "tok-123"
    at.session_state["user"] = {"username": "ana", "role": role, "permissions": PERMS[role], "expires_at": "x"}
    return at.run()


def test_dashboard_renders_kpis_and_tabs(monkeypatch):
    canned(monkeypatch)
    at = signed_in("analyst")
    assert not at.exception
    assert at.title[0].value == "Payment Operations Intelligence"
    labels = [m.label for m in at.metric]
    assert {"Payment attempts", "Failed", "Failure rate", "Affected transaction value"} <= set(labels)
    assert [t.label for t in at.tabs] == ["Trends", "Providers", "Failure reasons", "Incidents & timeline", "Investigate",
                                          "History"]


@pytest.mark.parametrize("role, extra_tabs", [("viewer", []), ("analyst", ["Investigate", "History"]),
                                              ("admin", ["History", "Audit log"])])
def test_tabs_follow_the_role(monkeypatch, role, extra_tabs):
    canned(monkeypatch)
    at = signed_in(role)
    assert not at.exception
    assert [t.label for t in at.tabs][4:] == extra_tabs


def test_without_a_session_only_the_sign_in_form_is_shown(monkeypatch):
    canned(monkeypatch)
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert [x.label for x in at.text_input] == ["Username", "Password"]
    assert not at.tabs and not at.metric                 # no data before signing in


def test_signing_in_keeps_the_token_in_this_session_only(monkeypatch):
    canned(monkeypatch)
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.text_input[0].input("ana")
    at.text_input[1].input("a long password 1")
    at.button[0].click().run()
    assert not at.exception
    assert at.session_state["token"] == "tok-123" and at.session_state["user"]["role"] == "analyst"
    assert at.tabs                                        # the dashboard is shown now


def test_an_expired_session_returns_to_sign_in(monkeypatch):
    canned(monkeypatch)

    def expired(self, *args, **kwargs):
        raise ApiError("Session expired. Please sign in again.", status=401)

    monkeypatch.setattr(ApiClient, "meta", expired)
    at = signed_in("analyst")
    assert not at.exception
    assert "token" not in at.session_state
    assert [x.label for x in at.text_input] == ["Username", "Password"]


def test_history_tab_lists_saved_investigations_and_offers_resume(monkeypatch):
    canned(monkeypatch)
    at = signed_in("analyst")
    assert not at.exception
    history = at.tabs[5]
    frames = [df.value for df in history.dataframe]
    # Look in the column itself: printing a whole table may hide middle columns as "...".
    assert any("question" in f.columns and f["question"].astype(str).str.contains("Why did failures rise").any()
               for f in frames), [list(f.columns) for f in frames]
    resume = next(b for b in history.button if b.label == "Resume from checkpoints")
    assert not resume.disabled                     # interrupted -> resumable


def test_dashboard_shows_a_clear_error_when_the_api_is_down(monkeypatch):
    canned(monkeypatch, broken=True)
    at = signed_in("analyst")
    assert not at.exception
    assert "Cannot reach the API" in at.error[0].value
