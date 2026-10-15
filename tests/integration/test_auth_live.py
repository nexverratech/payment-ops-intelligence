"""Sign-in, permissions and the audit log against the real database, through the real API.

Skipped unless APP_DB_PASSWORD is set and `python -m payops.db.setup_app` has run.
Creates temporary accounts named itest_*, removed afterwards with the owner login.
The audit rows these tests write stay (the log is append-only by design); their actor
starts with "itest_".
"""

import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from payops import health
from payops.api.main import app
from payops.config import get_settings
from payops.db.connection import get_engine, get_history_engine
from payops.security import service
from payops.security.audit import verify_chain
from payops.security.store import PostgresAuthStore

pytestmark = pytest.mark.integration
PW = "integration test passphrase"


@pytest.fixture(scope="module")
def settings():
    s = get_settings()
    if health.check_database(s).status != "ok":
        pytest.skip("PostgreSQL is not reachable")
    if not s.history_enabled:
        pytest.skip("APP_DB_PASSWORD not set")
    with get_engine(s).connect() as c:
        if c.execute(text("SELECT to_regclass('ops.audit_log')")).scalar() is None:
            pytest.skip("Security tables missing: python -m payops.db.setup_app")
        if c.execute(text("SELECT to_regclass('payments.dataset_runs')")).scalar() is None:
            pytest.skip("No dataset loaded")
    return s


@pytest.fixture(scope="module")
def users(settings):
    store = PostgresAuthStore(get_history_engine(settings))
    suffix = secrets.token_hex(3)
    names = {"analyst": f"itest_ana_{suffix}", "viewer": f"itest_vera_{suffix}"}
    for role, name in names.items():
        service.create_user(store, settings, name, role, PW)
    yield names
    with get_engine(settings).begin() as c:   # owner login; sessions go with their user (ON DELETE CASCADE)
        c.execute(text("DELETE FROM ops.users WHERE username LIKE :p"), {"p": f"itest_%_{suffix}"})


@pytest.fixture(scope="module")
def client(users):
    app.dependency_overrides.clear()          # the real sign-in, no test shortcuts
    return TestClient(app)


def login(client, name):
    r = client.post("/api/v1/auth/login", json={"username": name, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_real_sign_in_reaches_real_data(client, users):
    h = login(client, users["viewer"])
    assert client.get("/api/v1/kpis", headers=h).status_code == 200
    assert client.post("/api/v1/investigations", json={"question": "Why did failures rise last week?", "use_llm": False},
                       headers=h).status_code == 403                       # viewers may not run investigations
    assert client.get("/api/v1/kpis").status_code == 401


def test_logout_is_immediate(client, users):
    h = login(client, users["analyst"])
    assert client.post("/api/v1/auth/logout", headers=h).status_code == 204
    assert client.get("/api/v1/auth/me", headers=h).status_code == 401


def test_only_the_token_hash_is_stored(client, settings, users):
    token = client.post("/api/v1/auth/login", json={"username": users["analyst"], "password": PW}).json()["token"]
    with get_engine(settings).connect() as c:
        stored = c.execute(text("SELECT token_hash FROM ops.sessions WHERE username = :u"), {"u": users["analyst"]}).scalars().all()
        hashes = c.execute(text("SELECT password_hash FROM ops.users WHERE username = :u"), {"u": users["analyst"]}).scalar()
    assert token not in stored and service.token_hash(token) in stored
    assert hashes.startswith("scrypt$") and PW not in hashes


def test_the_audit_log_is_append_only_for_every_login(settings):
    app_engine, owner = get_history_engine(settings), get_engine(settings)
    for engine, expected in ((app_engine, "permission denied"), (owner, "append-only")):
        for sql in ("UPDATE ops.audit_log SET actor = 'x'", "DELETE FROM ops.audit_log"):
            with pytest.raises(DBAPIError) as info:
                with engine.begin() as c:
                    c.execute(text(sql))
            assert expected in str(info.value.orig).lower(), sql


def test_the_application_login_cannot_read_payment_data(settings):
    with pytest.raises(DBAPIError) as info:
        with get_history_engine(settings).connect() as c:
            c.execute(text("SELECT count(*) FROM payments.transactions"))
    assert "permission denied" in str(info.value.orig).lower()


def test_the_real_audit_chain_verifies(client, settings, users):
    login(client, users["viewer"])                   # at least one new event
    report = verify_chain(PostgresAuthStore(get_history_engine(settings)).iter_audit())
    assert report.ok, report
    assert report.rows >= 1
