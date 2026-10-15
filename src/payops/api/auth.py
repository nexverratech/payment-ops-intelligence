"""Sign-in and permission checks for every protected endpoint.

How a request is authorised (all in this backend; nothing is decided by the LLM or the browser):
1. `current_user` reads the `Authorization: Bearer <token>` header and looks the session up
   (security/service.py). No or bad token -> 401.
2. `require(permission)` checks the user's ROLE against security/rbac.py. Missing -> 403,
   and the denial is written to the audit log.
3. Endpoints that touch one investigation also check OWNERSHIP (routes_investigations.py).

Fail closed: if the application database login is not configured, every protected
endpoint answers 503 instead of running unauthenticated.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from payops.config import Settings, get_settings
from payops.db.connection import get_history_engine
from payops.security import service
from payops.security.audit import AuditEvent, DetailValue
from payops.security.rbac import Permission
from payops.security.service import AuthError, Principal
from payops.security.store import AuthStore, PostgresAuthStore

log = logging.getLogger("payops.api.auth")
_bearer = HTTPBearer(auto_error=False, description="Session token from POST /api/v1/auth/login")

NOT_CONFIGURED = ("Sign-in is not configured: set APP_DB_PASSWORD in .env, run python -m payops.db.setup_app, "
                  "and create a user with python -m payops.security.manage create-user")


def get_auth_store(settings: Annotated[Settings, Depends(get_settings)]) -> AuthStore | None:
    return PostgresAuthStore(get_history_engine(settings)) if settings.history_enabled else None


AuthStoreDep = Annotated[AuthStore | None, Depends(get_auth_store)]


def require_store(store: AuthStore | None) -> AuthStore:
    if store is None:
        raise HTTPException(503, detail=NOT_CONFIGURED)
    return store


def current_user(settings: Annotated[Settings, Depends(get_settings)], store: AuthStoreDep,
                 creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> Principal:
    store = require_store(store)
    if creds is None:
        raise HTTPException(401, detail="Not signed in.", headers={"WWW-Authenticate": "Bearer"})
    try:
        return service.resolve(store, settings, creds.credentials)
    except AuthError as exc:
        raise HTTPException(401, detail=exc.public, headers={"WWW-Authenticate": "Bearer"}) from None


CurrentUser = Annotated[Principal, Depends(current_user)]


def audit(store: AuthStore | None, actor: Principal | str, action: str, *, target: str | None = None,
          outcome: str = "success", details: dict[str, DetailValue] | None = None) -> None:
    """Write one audit event. If writing fails, the error propagates and the request fails:
    no audit record, no action."""
    store = require_store(store)
    name, role = (actor.username, actor.role) if isinstance(actor, Principal) else (actor, None)
    store.append_audit(AuditEvent(actor=name, role=role, action=action, target=target, outcome=outcome,  # type: ignore[arg-type]
                                  source="api", details=details or {}))


def deny(store: AuthStore | None, user: Principal, request: Request, permission: str) -> HTTPException:
    audit(store, user, "access.denied", target=f"{request.method} {request.url.path}", outcome="denied",
          details={"permission": permission})
    log.warning("access denied", extra={"fields": {"user": user.username, "role": user.role, "permission": permission}})
    return HTTPException(403, detail=f"Your role ({user.role}) does not allow this ({permission}).")


def require(*any_of: Permission) -> Callable[..., Principal]:
    """Dependency: the signed-in user must hold at least one of these permissions."""

    def check(request: Request, user: CurrentUser, store: AuthStoreDep) -> Principal:
        if any(user.can(p) for p in any_of):
            return user
        raise deny(store, user, request, " or ".join(p.value for p in any_of))

    return check
