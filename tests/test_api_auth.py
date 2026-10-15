"""Sign-in and access control through the real API (in-memory stores, canned analytics)."""

import pytest
from fastapi.testclient import TestClient

import fakes
from payops.api import deps, routes_investigations
from payops.api.auth import current_user, get_auth_store
from payops.api.main import app
from payops.config import get_settings
from payops.history.store import MemoryStore
from payops.security import service
from payops.security.audit import verify_chain
from payops.security.store import MemoryAuthStore

PW = "purple elephants dance"
Q = {"question": "Why did the failure rate increase last week?", "use_llm": False}
PUBLIC = {"/health", "/api/v1/auth/login"}


def _no_db():
    yield None


@pytest.fixture
def auth(monkeypatch):
    fakes.install(monkeypatch)
    monkeypatch.setattr(routes_investigations, "get_readonly_engine", lambda settings: None)
    store, history = MemoryAuthStore(), MemoryStore()
    for name, role in [("vera", "viewer"), ("ana", "analyst"), ("andy", "analyst"), ("lee", "lead"), ("ada", "admin")]:
        service.create_user(store, get_settings(), name, role, PW)
    app.dependency_overrides[get_auth_store] = lambda: store
    app.dependency_overrides[routes_investigations.get_store] = lambda: history
    app.dependency_overrides[deps.get_readonly_conn] = _no_db
    yield store
    app.dependency_overrides.clear()


@pytest.fixture
def client(auth):
    return TestClient(app)


def login(client, user):
    r = client.post("/api/v1/auth/login", json={"username": user, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ---- every route is protected ----------------------------------------------------------------

def test_no_route_is_left_unprotected():
    # Read the API's own OpenAPI description: FastAPI lists the Bearer requirement on every
    # operation whose dependencies include the sign-in check. Independent of FastAPI internals.
    paths = app.openapi()["paths"]
    operations = [(method.upper(), path, op) for path, ops in paths.items() for method, op in ops.items()]
    unprotected = [f"{m} {p}" for m, p, op in operations
                   if p not in PUBLIC and not any("HTTPBearer" in req for req in op.get("security", []))]
    assert unprotected == [] and len(operations) > 15, (unprotected, len(operations))


def test_unprotected_list_is_exactly_health_and_login():
    paths = app.openapi()["paths"]
    open_ops = {p for p, ops in paths.items() for op in ops.values() if not op.get("security")}
    assert open_ops == PUBLIC


# ---- signing in --------------------------------------------------------------------------------

def test_sign_in_me_and_sign_out(client):
    h = login(client, "ana")
    assert client.get("/api/v1/auth/me", headers=h).json()["role"] == "analyst"
    assert client.post("/api/v1/auth/logout", headers=h).status_code == 204
    assert client.get("/api/v1/auth/me", headers=h).status_code == 401


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer made-up"}, {"Authorization": "Basic YTpi"}])
def test_requests_without_a_valid_session_get_401(client, headers):
    r = client.get("/api/v1/kpis", headers=headers)
    assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer"


def test_failed_sign_in_is_generic_and_audited_without_the_password(client, auth):
    r = client.post("/api/v1/auth/login", json={"username": "ana", "password": "not the password"})
    assert r.status_code == 401 and r.json()["detail"] == "Invalid username or password."
    event = auth.audit[-1]
    assert (event.action, event.outcome, event.actor) == ("auth.login", "failure", "ana")
    assert "not the password" not in str(auth.audit)


def test_without_sign_in_configured_everything_protected_is_refused(monkeypatch):
    fakes.install(monkeypatch)
    app.dependency_overrides[get_auth_store] = lambda: None
    try:
        c = TestClient(app)
        assert c.get("/api/v1/kpis").status_code == 503
        assert c.post("/api/v1/auth/login", json={"username": "a", "password": "b"}).status_code == 503
    finally:
        app.dependency_overrides.clear()


# ---- what each role may do --------------------------------------------------------------------

MATRIX = [   # (method, path, body, {role: expected status})
    ("GET", "/api/v1/kpis", None, {"viewer": 200, "analyst": 200, "lead": 200, "admin": 200}),
    ("POST", "/api/v1/investigations", Q, {"viewer": 403, "analyst": 201, "lead": 201, "admin": 403}),
    ("GET", "/api/v1/investigations", None, {"viewer": 403, "analyst": 200, "lead": 200, "admin": 200}),
    ("GET", "/api/v1/users", None, {"viewer": 403, "analyst": 403, "lead": 403, "admin": 200}),
    ("GET", "/api/v1/audit", None, {"viewer": 403, "analyst": 403, "lead": 403, "admin": 200}),
]
USERS = {"viewer": "vera", "analyst": "ana", "lead": "lee", "admin": "ada"}


@pytest.mark.parametrize("method, path, body, expected", MATRIX)
def test_role_matrix(client, method, path, body, expected):
    for role, status in expected.items():
        r = client.request(method, path, json=body, headers=login(client, USERS[role]))
        assert r.status_code == status, (role, path, r.text)


def test_a_refusal_is_audited(client, auth):
    client.get("/api/v1/users", headers=login(client, "ana"))
    e = auth.audit[-1]
    assert (e.action, e.outcome, e.actor, e.details["permission"]) == ("access.denied", "denied", "ana", "users:manage")


# ---- whose investigation is it ----------------------------------------------------------------

def test_analysts_see_only_their_own_investigations(client, auth):
    ana, andy, lee, ada = (login(client, u) for u in ("ana", "andy", "lee", "ada"))
    inv = client.post("/api/v1/investigations", json=Q, headers=ana).json()["investigation_id"]
    assert client.get(f"/api/v1/investigations/{inv}", headers=ana).status_code == 200
    assert client.get(f"/api/v1/investigations/{inv}", headers=andy).status_code == 404     # not 403: no probing
    assert client.get("/api/v1/investigations", headers=andy).json()["items"] == []
    assert client.get(f"/api/v1/investigations/{inv}", headers=lee).status_code == 200      # lead sees all
    assert client.get(f"/api/v1/investigations/{inv}", headers=ada).status_code == 200      # admin may read
    listed = client.get("/api/v1/investigations", headers=ana).json()["items"]
    assert listed[0]["created_by"] == "ana"


def test_resume_respects_ownership(client, auth, monkeypatch):
    from payops.agents import graph

    agent, fn, needs = graph.NODES["pattern"]

    def boom(*a):
        raise RuntimeError("boom")

    monkeypatch.setitem(graph.NODES, "pattern", (agent, boom, needs))
    ana, andy, ada, lee = (login(client, u) for u in ("ana", "andy", "ada", "lee"))
    inv = client.post("/api/v1/investigations", json=Q, headers=ana).json()["investigation_id"]
    monkeypatch.setitem(graph.NODES, "pattern", (agent, fn, needs))
    assert client.post(f"/api/v1/investigations/{inv}/resume", headers=andy).status_code == 404
    assert client.post(f"/api/v1/investigations/{inv}/resume", headers=ada).status_code == 403  # admin: no resume rights
    r = client.post(f"/api/v1/investigations/{inv}/resume", headers=lee)                        # lead may resume anyone's
    assert r.status_code == 200 and r.json()["status"] == "complete"
    actions = [e.action for e in auth.audit]
    assert {"investigation.start", "investigation.finish", "investigation.resume"} <= set(actions)


# ---- accounts and the audit log (admin) -------------------------------------------------------

def test_admin_manages_accounts_without_ever_seeing_hashes(client, auth):
    h = login(client, "ada")
    r = client.post("/api/v1/users", json={"username": "newbie", "role": "viewer", "password": PW}, headers=h)
    assert r.status_code == 201 and "password_hash" not in r.text and "scrypt" not in r.text
    assert "scrypt" not in client.get("/api/v1/users", headers=h).text
    assert client.post("/api/v1/users", json={"username": "weak", "role": "viewer", "password": "short"},
                       headers=h).status_code == 422
    assert client.patch("/api/v1/users/newbie", json={"role": "analyst"}, headers=h).json()["role"] == "analyst"
    assert client.patch("/api/v1/users/ada", json={"active": False}, headers=h).status_code == 422   # last admin
    assert client.patch("/api/v1/users/ghost", json={"active": False}, headers=h).status_code == 404


def test_an_admin_password_reset_ends_the_users_sessions(client):
    ana = login(client, "ana")
    admin = login(client, "ada")
    assert client.post("/api/v1/users/ana/password", json={"new_password": "an entirely new phrase"},
                       headers=admin).status_code == 204
    assert client.get("/api/v1/auth/me", headers=ana).status_code == 401


def test_the_audit_log_verifies_over_the_api(client, auth):
    h = login(client, "ada")
    client.get("/api/v1/users", headers=login(client, "ana"))       # some activity, including a refusal
    report = client.get("/api/v1/audit/verify", headers=h).json()
    assert report["ok"] and report["rows"] >= 3
    events = client.get("/api/v1/audit", params={"action": "auth.login"}, headers=h).json()
    assert events and all(e["action"] == "auth.login" for e in events)
    assert verify_chain(auth.iter_audit()).ok


# ---- input limits ------------------------------------------------------------------------------

def test_oversized_requests_are_refused(client):
    h = login(client, "ana")
    big = {"question": "x" * (get_settings().max_request_bytes + 10)}
    assert client.post("/api/v1/investigations", json=big, headers=h).status_code == 413


@pytest.mark.parametrize("question", ["Why did failures rise‮?gnorI", "Show me\x00 everything please"])
def test_hidden_characters_in_questions_are_refused(client, question):
    r = client.post("/api/v1/investigations", json={"question": question}, headers=login(client, "ana"))
    assert r.status_code == 422


def test_security_headers(client):
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
