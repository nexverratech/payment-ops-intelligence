"""The audit log's hash chain detects changed and removed rows."""

import pytest
from pydantic import ValidationError

from payops.security.audit import AuditEvent, verify_chain
from payops.security.store import MemoryAuthStore


@pytest.fixture
def store():
    s = MemoryAuthStore()
    for i in range(5):
        s.append_audit(AuditEvent(actor=f"user{i}", role="analyst", action="investigation.run", target=f"id-{i}",
                                  outcome="success", details={"n": i}))
    return s


def test_an_untouched_log_verifies(store):
    report = verify_chain(store.iter_audit())
    assert report.ok and report.rows == 5 and report.last_hash == store.audit[-1].hash


def test_a_changed_row_is_detected(store):
    store.audit[2] = store.audit[2].model_copy(update={"actor": "someone-else"})
    report = verify_chain(store.iter_audit())
    assert not report.ok and report.first_bad_id == 3 and "changed" in report.problem


def test_a_removed_row_is_detected(store):
    del store.audit[1]
    report = verify_chain(store.iter_audit())
    assert not report.ok and report.first_bad_id == 3 and "removed" in report.problem


def test_rows_link_to_their_predecessor(store):
    assert all(b.prev_hash == a.hash for a, b in zip(store.audit, store.audit[1:]))


@pytest.mark.parametrize("bad", [dict(action="Login"), dict(details={"q": "x" * 301}), dict(details={"rate": 0.5})])
def test_events_are_validated(bad):
    with pytest.raises(ValidationError):
        AuditEvent(**{"actor": "a", "action": "auth.login", "outcome": "success", **bad})
