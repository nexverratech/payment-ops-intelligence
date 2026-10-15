"""Sign in, sign out, who am I, change my password; and (admin) accounts and the audit log.

POST  /api/v1/auth/login                    {"username", "password"} -> token (the only unauthenticated route besides /health)
POST  /api/v1/auth/logout
GET   /api/v1/auth/me
POST  /api/v1/auth/password                 {"current_password", "new_password"}
GET   /api/v1/users                         admin
POST  /api/v1/users                         admin   {"username", "role", "password"}
PATCH /api/v1/users/{username}              admin   {"role"?, "active"?}
POST  /api/v1/users/{username}/password     admin   {"new_password"}   (reset)
POST  /api/v1/users/{username}/unlock       admin
GET   /api/v1/audit                         admin   ?limit&offset&action&actor
GET   /api/v1/audit/verify                  admin   recompute the hash chain
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, Field

from payops.api.auth import AuthStoreDep, CurrentUser, audit, require, require_store
from payops.config import Settings, get_settings
from payops.security import service
from payops.security.audit import ChainReport, verify_chain
from payops.security.passwords import PasswordPolicyError
from payops.security.rbac import Permission, permissions_for
from payops.security.service import AccountError, AuthError, Principal
from payops.security.store import AuditRecord

router = APIRouter(prefix="/api/v1", tags=["auth"])
Config = Annotated[Settings, Depends(get_settings)]
Username = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_.-]{2,31}$")]
Role = Literal["viewer", "analyst", "lead", "admin"]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class Me(BaseModel):
    username: str
    role: str
    permissions: list[str]


class LoginResponse(Me):
    token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class NewUser(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    role: Role
    password: str = Field(min_length=1, max_length=256)


class UserChange(BaseModel):
    role: Role | None = None
    active: bool | None = None


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    username: str
    role: str
    active: bool
    failed_logins: int
    locked_until: datetime | None
    created_at: datetime
    password_changed_at: datetime


def _me(p: Principal) -> Me:
    return Me(username=p.username, role=p.role, permissions=sorted(x.value for x in permissions_for(p.role)))


# ---- signing in ----------------------------------------------------------------------------

@router.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest, settings: Config, store: AuthStoreDep) -> LoginResponse:
    store = require_store(store)
    name = body.username.strip().lower()
    actor = name if service.valid_username(name) else "anonymous"
    try:
        token, expires, principal = service.login(store, settings, body.username, body.password)
    except AuthError as exc:
        audit(store, actor, "auth.login", outcome="failure", details={"reason": exc.reason})
        raise HTTPException(401, detail=exc.public) from None
    audit(store, principal, "auth.login")
    return LoginResponse(token=token, expires_at=expires, **_me(principal).model_dump())


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(user: CurrentUser, store: AuthStoreDep) -> Response:
    service.logout(require_store(store), user)
    audit(store, user, "auth.logout")
    return Response(status_code=204)


@router.get("/auth/me", response_model=Me)
def me(user: CurrentUser) -> Me:
    return _me(user)


@router.post("/auth/password", status_code=status.HTTP_204_NO_CONTENT)
def change_own_password(body: PasswordChange, user: CurrentUser, settings: Config, store: AuthStoreDep) -> Response:
    try:
        service.set_password(require_store(store), settings, user.username, body.new_password, body.current_password)
    except (AccountError, PasswordPolicyError) as exc:
        audit(store, user, "auth.password_change", outcome="failure", details={"reason": str(exc)})
        raise HTTPException(422, detail=str(exc)) from None
    audit(store, user, "auth.password_change", details={"sessions_ended": True})
    return Response(status_code=204)


# ---- accounts (admin) ------------------------------------------------------------------------

Admin = Annotated[Principal, Depends(require(Permission.USERS_MANAGE))]


@router.get("/users", response_model=list[UserOut])
def list_users(admin: Admin, store: AuthStoreDep) -> list[UserOut]:
    return [UserOut(**u.model_dump()) for u in require_store(store).list_users()]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: NewUser, admin: Admin, settings: Config, store: AuthStoreDep) -> UserOut:
    store = require_store(store)
    try:
        service.create_user(store, settings, body.username, body.role, body.password)
    except (AccountError, PasswordPolicyError) as exc:
        raise HTTPException(422, detail=str(exc)) from None
    audit(store, admin, "user.create", target=body.username.strip().lower(), details={"role": body.role})
    return UserOut(**store.get_user(body.username.strip().lower()).model_dump())  # type: ignore[union-attr]


@router.patch("/users/{username}", response_model=UserOut)
def update_user(username: Username, body: UserChange, admin: Admin, store: AuthStoreDep) -> UserOut:
    store = require_store(store)
    try:
        service.change_role_or_status(store, username, body.role, body.active)
    except AccountError as exc:
        raise HTTPException(404 if "no user" in str(exc) else 422, detail=str(exc)) from None
    audit(store, admin, "user.update", target=username,
          details={"role": body.role, "active": body.active})
    return UserOut(**store.get_user(username).model_dump())  # type: ignore[union-attr]


@router.post("/users/{username}/password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(username: Username, body: PasswordReset, admin: Admin, settings: Config,
                   store: AuthStoreDep) -> Response:
    try:
        service.set_password(require_store(store), settings, username, body.new_password)
    except (AccountError, PasswordPolicyError) as exc:
        raise HTTPException(404 if "no user" in str(exc) else 422, detail=str(exc)) from None
    audit(store, admin, "user.password_reset", target=username, details={"sessions_ended": True})
    return Response(status_code=204)


@router.post("/users/{username}/unlock", status_code=status.HTTP_204_NO_CONTENT)
def unlock(username: Username, admin: Admin, store: AuthStoreDep) -> Response:
    store = require_store(store)
    if store.get_user(username) is None:
        raise HTTPException(404, detail=f"no user '{username}'")
    store.record_login_success(username)
    audit(store, admin, "user.unlock", target=username)
    return Response(status_code=204)


# ---- audit log (admin) -----------------------------------------------------------------------

Auditor = Annotated[Principal, Depends(require(Permission.AUDIT_READ))]


@router.get("/audit", response_model=list[AuditRecord])
def read_audit(auditor: Auditor, store: AuthStoreDep,
               limit: Annotated[int, Query(ge=1, le=500)] = 100,
               offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
               action: Annotated[str | None, Query(pattern=r"^[a-z_]+\.[a-z_]+$")] = None,
               actor: Annotated[str | None, Query(max_length=80)] = None) -> list[AuditRecord]:
    return require_store(store).list_audit(limit=limit, offset=offset, action=action, actor=actor)


@router.get("/audit/verify", response_model=ChainReport)
def verify_audit(auditor: Auditor, store: AuthStoreDep) -> ChainReport:
    report = verify_chain(require_store(store).iter_audit())
    audit(store, auditor, "audit.verify", outcome="success" if report.ok else "failure",
          details={"rows": report.rows, "ok": report.ok})
    return report
