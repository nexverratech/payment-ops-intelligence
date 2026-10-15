"""Authentication and account rules, independent of the web framework (used by the API and the CLI).

Login:
* Wrong password or unknown user -> the same generic error, after the same amount of
  work (a dummy hash is checked for unknown users), so usernames cannot be probed.
* LOGIN_MAX_FAILURES wrong passwords in a row lock the account for
  LOGIN_LOCKOUT_MINUTES. A locked or disabled account is refused even with the
  right password.
* A stored hash with older (weaker) parameters is upgraded at the next login.

Sessions:
* 256-bit random token (`secrets.token_urlsafe(32)`); only its SHA-256 is stored.
* Ends at the first of: logout, idle for SESSION_IDLE_MINUTES, SESSION_MAX_HOURS
  after login, account disabled, password changed or reset.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from payops.config import Settings
from payops.security import passwords
from payops.security.rbac import ROLES, Permission, permissions_for
from payops.security.store import AuthStore, UserExists, UnknownUser, utcnow

USERNAME = re.compile(r"^[a-z][a-z0-9_.-]{2,31}$")


class AuthError(Exception):
    """Login or session problem. `public` is what the user may see; `reason` goes to the audit log."""

    def __init__(self, public: str, reason: str):
        super().__init__(public)
        self.public, self.reason = public, reason


class AccountError(ValueError):
    """An account change that is not allowed; the message is safe to show."""


@dataclass(frozen=True)
class Principal:
    username: str
    role: str
    permissions: frozenset[Permission]
    token_hash: str | None = None      # None for the CLI

    def can(self, p: Permission) -> bool:
        return p in self.permissions


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_username(username: str) -> bool:
    return bool(USERNAME.match(username or ""))


# ---- login and sessions ------------------------------------------------------------------------

GENERIC = "Invalid username or password."


def login(store: AuthStore, settings: Settings, username: str, password: str) -> tuple[str, datetime, Principal]:
    """Check credentials and start a session. Returns (token, absolute expiry, principal)."""
    username = (username or "").strip().lower()
    user = store.get_user(username) if valid_username(username) else None
    if user is None:
        passwords.burn_time(password)
        raise AuthError(GENERIC, "unknown user")
    now = utcnow()
    if user.locked_until and user.locked_until > now:
        passwords.burn_time(password)
        raise AuthError("Account temporarily locked after repeated failed logins. Try again later.", "locked")
    if not passwords.verify_password(password, user.password_hash):
        failures = user.failed_logins + 1
        lock = now + timedelta(minutes=settings.login_lockout_minutes) if failures >= settings.login_max_failures else None
        store.record_login_failure(username, lock)
        raise AuthError(GENERIC, "account locked after repeated failures" if lock else f"wrong password ({failures})")
    if not user.active:
        raise AuthError(GENERIC, "account disabled")
    store.record_login_success(username)
    if passwords.needs_rehash(user.password_hash):
        store.set_password(username, passwords.hash_password(password))
    token = secrets.token_urlsafe(32)
    expires = now + timedelta(hours=settings.session_max_hours)
    store.create_session(token_hash(token), username, expires)
    return token, expires, Principal(username, user.role, permissions_for(user.role), token_hash(token))


def resolve(store: AuthStore, settings: Settings, token: str) -> Principal:
    """The signed-in user for a token, or AuthError. Refreshes the idle timer."""
    if not token or len(token) > 200:
        raise AuthError("Not signed in.", "missing or malformed token")
    th = token_hash(token)
    s = store.get_session(th)
    now = utcnow()
    if s is None:
        raise AuthError("Not signed in.", "unknown token")
    if s.revoked_at is not None:
        raise AuthError("Session ended. Please sign in again.", "session revoked")
    if now >= s.expires_at:
        raise AuthError("Session expired. Please sign in again.", "session past maximum age")
    if now - s.last_seen_at > timedelta(minutes=settings.session_idle_minutes):
        store.revoke_session(th)
        raise AuthError("Session expired after inactivity. Please sign in again.", "session idle")
    if not s.active:
        store.revoke_session(th)
        raise AuthError("Account disabled.", "account disabled")
    store.touch_session(th)
    return Principal(s.username, s.role, permissions_for(s.role), th)


def logout(store: AuthStore, principal: Principal) -> None:
    if principal.token_hash:
        store.revoke_session(principal.token_hash)


# ---- account management ------------------------------------------------------------------------

def create_user(store: AuthStore, settings: Settings, username: str, role: str, password: str) -> None:
    username = username.strip().lower()
    if not valid_username(username):
        raise AccountError("username: 3-32 characters, lowercase letters, digits, '.', '_' or '-', starting with a letter")
    if role not in ROLES:
        raise AccountError(f"role must be one of {', '.join(ROLES)}")
    passwords.check_policy(password, username, settings.password_min_length)
    try:
        store.create_user(username, role, passwords.hash_password(password))
    except UserExists as exc:
        raise AccountError(str(exc)) from None


def change_role_or_status(store: AuthStore, username: str, role: str | None, active: bool | None) -> None:
    user = store.get_user(username)
    if user is None:
        raise AccountError(f"no user '{username}'")
    if role is not None and role not in ROLES:
        raise AccountError(f"role must be one of {', '.join(ROLES)}")
    loses_admin = user.role == "admin" and user.active and ((role is not None and role != "admin") or active is False)
    if loses_admin and store.count_active_admins() <= 1:
        raise AccountError("this is the last active admin; create another admin first")
    store.update_user(username, role=role, active=active)
    if active is False or (role is not None and role != user.role):
        store.revoke_user_sessions(username)       # new rights apply from a fresh sign-in


def set_password(store: AuthStore, settings: Settings, username: str, new_password: str,
                 current_password: str | None = None) -> None:
    """Own change (current_password given) or admin reset (None). Ends all the user's sessions."""
    user = store.get_user(username)
    if user is None:
        raise AccountError(f"no user '{username}'")
    if current_password is not None and not passwords.verify_password(current_password, user.password_hash):
        raise AccountError("current password is wrong")
    passwords.check_policy(new_password, username, settings.password_min_length)
    try:
        store.set_password(username, passwords.hash_password(new_password))
    except UnknownUser:
        raise AccountError(f"no user '{username}'") from None
    store.revoke_user_sessions(username)
