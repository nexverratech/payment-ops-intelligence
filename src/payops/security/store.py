"""Where accounts, sessions and the audit log are kept (schema "ops", application login).

* PostgresAuthStore: the real one.
* MemoryAuthStore:   for tests, same behaviour (including the append-only audit chain).

Sessions are server-side: the browser or CLI holds a random token, the database
holds only its SHA-256 hash. Logging out, disabling an account or resetting a
password therefore takes effect on the very next request.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy import Engine, text

from payops.history.codec import as_dict, encode_json
from payops.security.audit import GENESIS, AuditEvent, AuditRecord, chain_hash


class UserExists(ValueError):
    pass


class UnknownUser(LookupError):
    pass


class UserRecord(BaseModel):
    username: str
    role: str
    active: bool
    password_hash: str
    failed_logins: int = 0
    locked_until: datetime | None = None
    created_at: datetime
    updated_at: datetime
    password_changed_at: datetime


class SessionInfo(BaseModel):
    """A session joined with its user's CURRENT role and status (role changes apply at once)."""

    username: str
    role: str
    active: bool
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuthStore(ABC):
    # users
    @abstractmethod
    def create_user(self, username: str, role: str, password_hash: str) -> None: ...
    @abstractmethod
    def get_user(self, username: str) -> UserRecord | None: ...
    @abstractmethod
    def list_users(self) -> list[UserRecord]: ...
    @abstractmethod
    def update_user(self, username: str, *, role: str | None = None, active: bool | None = None) -> None: ...
    @abstractmethod
    def set_password(self, username: str, password_hash: str) -> None: ...
    @abstractmethod
    def record_login_failure(self, username: str, lock_until: datetime | None) -> None: ...
    @abstractmethod
    def record_login_success(self, username: str) -> None: ...
    @abstractmethod
    def count_active_admins(self) -> int: ...
    # sessions
    @abstractmethod
    def create_session(self, token_hash: str, username: str, expires_at: datetime) -> None: ...
    @abstractmethod
    def get_session(self, token_hash: str) -> SessionInfo | None: ...
    @abstractmethod
    def touch_session(self, token_hash: str) -> None: ...
    @abstractmethod
    def revoke_session(self, token_hash: str) -> None: ...
    @abstractmethod
    def revoke_user_sessions(self, username: str) -> int: ...
    # audit
    @abstractmethod
    def append_audit(self, event: AuditEvent) -> AuditRecord: ...
    @abstractmethod
    def list_audit(self, limit: int = 50, offset: int = 0, action: str | None = None,
                   actor: str | None = None) -> list[AuditRecord]:
        """Newest first."""
    @abstractmethod
    def iter_audit(self) -> Iterator[AuditRecord]:
        """Oldest first (for chain verification)."""


# ---- PostgreSQL ------------------------------------------------------------------------------

_USER_COLS = "username, role, active, password_hash, failed_logins, locked_until, created_at, updated_at, password_changed_at"
_AUDIT_COLS = "id, at, actor, role, action, target, outcome, source, details, prev_hash, hash"
_AUDIT_LOCK = 734_221_001     # any constant: serialises audit inserts so the chain has no forks


class PostgresAuthStore(AuthStore):
    def __init__(self, engine: Engine):
        self._engine = engine

    def create_user(self, username, role, password_hash):
        with self._engine.begin() as c:
            if c.execute(text("SELECT 1 FROM ops.users WHERE username = :u"), {"u": username}).first():
                raise UserExists(f"user '{username}' already exists")
            c.execute(text("INSERT INTO ops.users (username, role, password_hash) VALUES (:u, :r, :h)"),
                      {"u": username, "r": role, "h": password_hash})

    def get_user(self, username):
        with self._engine.connect() as c:
            row = c.execute(text(f"SELECT {_USER_COLS} FROM ops.users WHERE username = :u"), {"u": username}).mappings().first()
        return UserRecord(**row) if row else None

    def list_users(self):
        with self._engine.connect() as c:
            rows = c.execute(text(f"SELECT {_USER_COLS} FROM ops.users ORDER BY username")).mappings().all()
        return [UserRecord(**r) for r in rows]

    def update_user(self, username, *, role=None, active=None):
        with self._engine.begin() as c:
            n = c.execute(text("UPDATE ops.users SET role = coalesce(:r, role), active = coalesce(:a, active), "
                               "updated_at = now() WHERE username = :u RETURNING 1"),
                          {"u": username, "r": role, "a": active}).first()
        if n is None:
            raise UnknownUser(username)

    def set_password(self, username, password_hash):
        with self._engine.begin() as c:
            n = c.execute(text("UPDATE ops.users SET password_hash = :h, password_changed_at = now(), updated_at = now(), "
                               "failed_logins = 0, locked_until = NULL WHERE username = :u RETURNING 1"),
                          {"u": username, "h": password_hash}).first()
        if n is None:
            raise UnknownUser(username)

    def record_login_failure(self, username, lock_until):
        with self._engine.begin() as c:
            c.execute(text("UPDATE ops.users SET failed_logins = CASE WHEN CAST(:lock AS timestamptz) IS NULL "
                           "THEN failed_logins + 1 ELSE 0 END, locked_until = coalesce(:lock, locked_until) "
                           "WHERE username = :u"), {"u": username, "lock": lock_until})

    def record_login_success(self, username):
        with self._engine.begin() as c:
            c.execute(text("UPDATE ops.users SET failed_logins = 0, locked_until = NULL WHERE username = :u"),
                      {"u": username})

    def count_active_admins(self):
        with self._engine.connect() as c:
            return int(c.execute(text("SELECT count(*) FROM ops.users WHERE role = 'admin' AND active")).scalar())

    def create_session(self, token_hash, username, expires_at):
        with self._engine.begin() as c:
            c.execute(text("INSERT INTO ops.sessions (token_hash, username, expires_at) VALUES (:t, :u, :e)"),
                      {"t": token_hash, "u": username, "e": expires_at})

    def get_session(self, token_hash):
        with self._engine.connect() as c:
            row = c.execute(text("""
                SELECT s.username, u.role, u.active, s.created_at, s.last_seen_at, s.expires_at, s.revoked_at
                FROM ops.sessions s JOIN ops.users u USING (username) WHERE s.token_hash = :t"""),
                {"t": token_hash}).mappings().first()
        return SessionInfo(**row) if row else None

    def touch_session(self, token_hash):
        with self._engine.begin() as c:
            c.execute(text("UPDATE ops.sessions SET last_seen_at = now() WHERE token_hash = :t"), {"t": token_hash})

    def revoke_session(self, token_hash):
        with self._engine.begin() as c:
            c.execute(text("UPDATE ops.sessions SET revoked_at = now() WHERE token_hash = :t AND revoked_at IS NULL"),
                      {"t": token_hash})

    def revoke_user_sessions(self, username):
        with self._engine.begin() as c:
            rows = c.execute(text("UPDATE ops.sessions SET revoked_at = now() WHERE username = :u AND revoked_at IS NULL "
                                  "RETURNING 1"), {"u": username}).all()
        return len(rows)

    def append_audit(self, event):
        at = utcnow()
        with self._engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _AUDIT_LOCK})
            prev = c.execute(text("SELECT hash FROM ops.audit_log ORDER BY id DESC LIMIT 1")).scalar() or GENESIS
            h = chain_hash(prev, at, event)
            new_id = c.execute(text("""
                INSERT INTO ops.audit_log (at, actor, role, action, target, outcome, source, details, prev_hash, hash)
                VALUES (:at, :actor, :role, :action, :target, :outcome, :source, CAST(:details AS jsonb), :prev, :hash)
                RETURNING id"""), {"at": at, **event.model_dump(exclude={"details"}), "details": encode_json(event.details),
                                    "prev": prev, "hash": h}).scalar()
        return AuditRecord(id=new_id, at=at, prev_hash=prev, hash=h, **event.model_dump())

    def list_audit(self, limit=50, offset=0, action=None, actor=None):
        with self._engine.connect() as c:
            rows = c.execute(text(f"SELECT {_AUDIT_COLS} FROM ops.audit_log "
                                  "WHERE (CAST(:action AS text) IS NULL OR action = :action) "
                                  "AND (CAST(:actor AS text) IS NULL OR actor = :actor) "
                                  "ORDER BY id DESC LIMIT :limit OFFSET :offset"),
                             {"action": action, "actor": actor, "limit": limit, "offset": offset}).mappings().all()
        return [_audit(r) for r in rows]

    def iter_audit(self):
        last = 0
        while True:   # in pages, so a long log never has to fit in memory at once
            with self._engine.connect() as c:
                rows = c.execute(text(f"SELECT {_AUDIT_COLS} FROM ops.audit_log WHERE id > :last ORDER BY id LIMIT 1000"),
                                 {"last": last}).mappings().all()
            if not rows:
                return
            for r in rows:
                yield _audit(r)
            last = rows[-1]["id"]


def _audit(row) -> AuditRecord:
    return AuditRecord(**{**row, "details": as_dict(row["details"]) or {}, "prev_hash": str(row["prev_hash"]).strip(),
                          "hash": str(row["hash"]).strip()})


# ---- In memory (tests) -------------------------------------------------------------------------

class MemoryAuthStore(AuthStore):
    def __init__(self):
        self._lock = threading.RLock()
        self.users: dict[str, UserRecord] = {}
        self.sessions: dict[str, dict] = {}
        self.audit: list[AuditRecord] = []

    def create_user(self, username, role, password_hash):
        with self._lock:
            if username in self.users:
                raise UserExists(f"user '{username}' already exists")
            now = utcnow()
            self.users[username] = UserRecord(username=username, role=role, active=True, password_hash=password_hash,
                                              created_at=now, updated_at=now, password_changed_at=now)

    def get_user(self, username):
        with self._lock:
            u = self.users.get(username)
            return u.model_copy() if u else None

    def list_users(self):
        with self._lock:
            return [self.users[k].model_copy() for k in sorted(self.users)]

    def update_user(self, username, *, role=None, active=None):
        with self._lock:
            u = self._user(username)
            u.role = role if role is not None else u.role
            u.active = active if active is not None else u.active
            u.updated_at = utcnow()

    def set_password(self, username, password_hash):
        with self._lock:
            u = self._user(username)
            u.password_hash, u.password_changed_at, u.updated_at = password_hash, utcnow(), utcnow()
            u.failed_logins, u.locked_until = 0, None

    def record_login_failure(self, username, lock_until):
        with self._lock:
            u = self._user(username)
            if lock_until is None:
                u.failed_logins += 1
            else:
                u.failed_logins, u.locked_until = 0, lock_until

    def record_login_success(self, username):
        with self._lock:
            u = self._user(username)
            u.failed_logins, u.locked_until = 0, None

    def count_active_admins(self):
        with self._lock:
            return sum(1 for u in self.users.values() if u.role == "admin" and u.active)

    def create_session(self, token_hash, username, expires_at):
        now = utcnow()
        with self._lock:
            self.sessions[token_hash] = {"username": username, "created_at": now, "last_seen_at": now,
                                         "expires_at": expires_at, "revoked_at": None}

    def get_session(self, token_hash):
        with self._lock:
            s = self.sessions.get(token_hash)
            if s is None or s["username"] not in self.users:
                return None
            u = self.users[s["username"]]
            return SessionInfo(role=u.role, active=u.active, **s)

    def touch_session(self, token_hash):
        with self._lock:
            if token_hash in self.sessions:
                self.sessions[token_hash]["last_seen_at"] = utcnow()

    def revoke_session(self, token_hash):
        with self._lock:
            s = self.sessions.get(token_hash)
            if s and s["revoked_at"] is None:
                s["revoked_at"] = utcnow()

    def revoke_user_sessions(self, username):
        with self._lock:
            live = [s for s in self.sessions.values() if s["username"] == username and s["revoked_at"] is None]
            for s in live:
                s["revoked_at"] = utcnow()
            return len(live)

    def append_audit(self, event):
        with self._lock:
            at = utcnow()
            prev = self.audit[-1].hash if self.audit else GENESIS
            rec = AuditRecord(id=len(self.audit) + 1, at=at, prev_hash=prev, hash=chain_hash(prev, at, event),
                              **event.model_dump())
            self.audit.append(rec)
            return rec

    def list_audit(self, limit=50, offset=0, action=None, actor=None):
        with self._lock:
            rows = [r for r in reversed(self.audit) if (action is None or r.action == action)
                    and (actor is None or r.actor == actor)]
            return rows[offset:offset + limit]

    def iter_audit(self):
        with self._lock:
            rows = list(self.audit)
        yield from rows

    def _user(self, username) -> UserRecord:
        if username not in self.users:
            raise UnknownUser(username)
        return self.users[username]
