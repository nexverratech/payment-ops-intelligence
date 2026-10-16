"""The account administration CLI (with an in-memory store)."""

import pytest

from payops.security import manage, service
from payops.security.audit import verify_chain
from payops.security.store import MemoryAuthStore

PW = "purple elephants dance"


@pytest.fixture
def store():
    return MemoryAuthStore()


def run(store, *argv, password=PW):
    return manage.main(list(argv), store=store, ask=lambda username: password)


def test_bootstrap_an_admin_and_audit_it(store, capsys):
    assert run(store, "create-user", "Alice", "--role", "admin") == 0
    assert store.get_user("alice").role == "admin"
    e = store.audit[-1]
    assert e.action == "user.create" and e.source == "cli" and e.actor.startswith("cli:")
    assert "alice" in capsys.readouterr().out


def test_weak_passwords_are_refused(store, capsys):
    assert run(store, "create-user", "bob", "--role", "analyst", password="short") == 1
    assert "Refused" in capsys.readouterr().out and store.get_user("bob") is None


def test_role_change_disable_and_unlock(store):
    run(store, "create-user", "alice", "--role", "admin")
    run(store, "create-user", "bob", "--role", "analyst")
    assert run(store, "set-role", "bob", "lead") == 0 and store.get_user("bob").role == "lead"
    assert run(store, "disable", "bob") == 0 and store.get_user("bob").active is False
    assert run(store, "disable", "alice") == 1                                # last admin
    store.record_login_failure("bob", service.utcnow())
    assert run(store, "unlock", "bob") == 0 and store.get_user("bob").locked_until is None


def test_verify_audit_reports_tampering(store, capsys):
    run(store, "create-user", "alice", "--role", "admin")
    run(store, "create-user", "bob", "--role", "viewer")
    assert run(store, "verify-audit") == 0 and "intact" in capsys.readouterr().out
    store.audit[0] = store.audit[0].model_copy(update={"target": "mallory"})
    assert run(store, "verify-audit") == 2 and "BROKEN at row 1" in capsys.readouterr().out
    assert not verify_chain(store.iter_audit()).ok


def test_password_from_stdin_for_scripts(store, monkeypatch):
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO(PW + "\n"))
    assert manage.main(["create-user", "ci-admin", "--role", "admin", "--password-stdin"], store=store) == 0
    from payops.security import passwords

    assert passwords.verify_password(PW, store.get_user("ci-admin").password_hash)
