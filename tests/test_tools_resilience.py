"""Tool calls: a temporary database error is retried once; timeouts and real errors are not; call limit."""

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError

import fakes
from payops.agents import tools as tools_module
from payops.agents.tools import MAX_CALLS_PER_STEP, AgentTools, ToolLimitError, is_transient
from payops.analytics import queries
from payops.analytics.models import AnalysisFilter


class PgError(Exception):
    def __init__(self, sqlstate):
        super().__init__("db error")
        self.sqlstate = sqlstate


def op_error(sqlstate="08006"):          # 08006 = connection failure
    return OperationalError("SELECT ...", {}, PgError(sqlstate))


F = AnalysisFilter(start=fakes.LAST_WEEK[0], end=fakes.LAST_WEEK[1])


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    fakes.install(monkeypatch)
    monkeypatch.setattr(tools_module, "RETRY_DELAYS_SECONDS", (0,))


def flaky(monkeypatch, errors):
    """queries.summary raises the given errors first, then answers."""
    pending = list(errors)

    def summary(conn, f):
        if pending:
            raise pending.pop(0)
        return fakes.summary(conn, f)

    monkeypatch.setattr(queries, "summary", summary)


def test_a_dropped_connection_is_retried_once_and_recorded(monkeypatch):
    flaky(monkeypatch, [op_error()])
    t = AgentTools("transaction", None)
    assert t.summary(F)["attempts"] == 1000
    assert t.calls[-1].status == "ok" and t.calls[-1].attempts == 2


def test_two_failures_in_a_row_are_reported(monkeypatch):
    flaky(monkeypatch, [op_error(), op_error()])
    t = AgentTools("transaction", None)
    with pytest.raises(OperationalError):
        t.summary(F)
    assert t.calls[-1].status == "failed" and t.calls[-1].attempts == 2


def test_a_statement_timeout_is_not_retried(monkeypatch):
    flaky(monkeypatch, [op_error("57014")])
    t = AgentTools("transaction", None)
    with pytest.raises(OperationalError):
        t.summary(F)
    assert t.calls[-1].attempts == 1


def test_a_real_sql_error_is_not_retried(monkeypatch):
    flaky(monkeypatch, [ProgrammingError("SELECT nonsense", {}, PgError("42601"))])
    t = AgentTools("transaction", None)
    with pytest.raises(ProgrammingError):
        t.summary(F)
    assert t.calls[-1].attempts == 1


def test_transient_classification():
    assert is_transient(op_error()) and not is_transient(op_error("57014"))
    assert not is_transient(ValueError("bad input"))


def test_an_agent_cannot_make_unlimited_calls():
    t = AgentTools("transaction", None)
    for _ in range(MAX_CALLS_PER_STEP):
        t.summary(F)
    with pytest.raises(ToolLimitError):
        t.summary(F)
