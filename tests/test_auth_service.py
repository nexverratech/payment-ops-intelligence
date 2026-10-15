"""Login, sessions and account rules (framework-independent)."""

from datetime import timedelta

import pytest

from payops.config import Settings
from payops.security import service
from payops.security.rbac import Permission
from payops.security.service import AccountError, AuthError
from payops.security.store import MemoryAuthStore

PW = "purple elephants dance"


@pytest.fixture
def settings():
    return Settings(_env_file=None, postgres_password="pw", readonly_db_password="ro")


@pytest.fixture
def store(settings):
    s = MemoryAuthStore()
    service.create_user(s, settings, "alice", "admin", PW)
    service.create_user(s, settings, "bob", "analyst", PW)
    return s


def test_login_gives_a_session_with_the_role_permissions(store, settings):
    token, expires, p = service.login(store, settings, "Bob", PW)        # usernames are case-insensitive
    assert p.username == "bob" and p.can(Permission.INVESTIGATION_RUN) and not p.can(Permission.USERS_MANAGE)
    assert len(token) >= 43 and token not in str(store.sessions)         # only the hash is stored
    assert service.resolve(store, settings, token).username == "bob"


@pytest.mark.parametrize("user, pw", [("bob", "wrong password!!"), ("nobody", PW), ("Robert'); DROP", PW)])
def test_wrong_password_and_unknown_user_look_the_same(store, settings, user, pw):
    with pytest.raises(AuthError) as info:
        service.login(store, settings, user, pw)
    assert info.value.public == service.GENERIC


def test_repeated_failures_lock_the_account_even_for_the_right_password(store, settings):
    for _ in range(settings.login_max_failures):
        with pytest.raises(AuthError):
            service.login(store, settings, "bob", "wrong password!!")
    with pytest.raises(AuthError, match="locked"):
        service.login(store, settings, "bob", PW)


def test_the_lock_expires(store, settings):
    store.record_login_failure("bob", service.utcnow() - timedelta(seconds=1))   # a lock that already ended
    assert service.login(store, settings, "bob", PW)[2].username == "bob"


def test_a_successful_login_resets_the_failure_count(store, settings):
    with pytest.raises(AuthError):
        service.login(store, settings, "bob", "wrong password!!")
    service.login(store, settings, "bob", PW)
    assert store.get_user("bob").failed_logins == 0


def test_disabled_accounts_cannot_sign_in_and_lose_their_sessions(store, settings):
    token, _, _ = service.login(store, settings, "bob", PW)
    service.change_role_or_status(store, "bob", None, False)
    with pytest.raises(AuthError):
        service.resolve(store, settings, token)
    with pytest.raises(AuthError):
        service.login(store, settings, "bob", PW)


def test_logout_ends_the_session(store, settings):
    token, _, p = service.login(store, settings, "bob", PW)
    service.logout(store, p)
    with pytest.raises(AuthError, match="ended"):
        service.resolve(store, settings, token)


def test_idle_sessions_expire(store, settings):
    token, _, _ = service.login(store, settings, "bob", PW)
    store.sessions[service.token_hash(token)]["last_seen_at"] -= timedelta(minutes=settings.session_idle_minutes + 1)
    with pytest.raises(AuthError, match="inactivity"):
        service.resolve(store, settings, token)


def test_sessions_have_a_maximum_age(store, settings):
    token, _, _ = service.login(store, settings, "bob", PW)
    store.sessions[service.token_hash(token)]["expires_at"] = service.utcnow() - timedelta(seconds=1)
    with pytest.raises(AuthError, match="expired"):
        service.resolve(store, settings, token)


@pytest.mark.parametrize("token", ["", "x" * 500, "made-up-token"])
def test_bad_tokens(store, settings, token):
    with pytest.raises(AuthError):
        service.resolve(store, settings, token)


def test_a_role_change_applies_and_ends_sessions(store, settings):
    token, _, _ = service.login(store, settings, "bob", PW)
    service.change_role_or_status(store, "bob", "viewer", None)
    with pytest.raises(AuthError):
        service.resolve(store, settings, token)
    _, _, p = service.login(store, settings, "bob", PW)
    assert p.role == "viewer" and not p.can(Permission.INVESTIGATION_RUN)


def test_password_change_needs_the_current_password_and_ends_sessions(store, settings):
    token, _, _ = service.login(store, settings, "bob", PW)
    with pytest.raises(AccountError, match="current password"):
        service.set_password(store, settings, "bob", "a brand new passphrase", current_password="wrong one!!")
    service.set_password(store, settings, "bob", "a brand new passphrase", current_password=PW)
    with pytest.raises(AuthError):
        service.resolve(store, settings, token)
    assert service.login(store, settings, "bob", "a brand new passphrase")


def test_the_last_admin_cannot_be_removed(store, settings):
    with pytest.raises(AccountError, match="last active admin"):
        service.change_role_or_status(store, "alice", "lead", None)
    with pytest.raises(AccountError, match="last active admin"):
        service.change_role_or_status(store, "alice", None, False)
    service.create_user(store, settings, "carol", "admin", PW)
    service.change_role_or_status(store, "alice", "lead", None)          # fine now: carol is still admin


@pytest.mark.parametrize("name, role", [("Al", "analyst"), ("bad name", "analyst"), ("dave", "root")])
def test_invalid_new_accounts(store, settings, name, role):
    with pytest.raises(AccountError):
        service.create_user(store, settings, name, role, PW)


def test_duplicate_account(store, settings):
    with pytest.raises(AccountError, match="exists"):
        service.create_user(store, settings, "bob", "viewer", PW)
