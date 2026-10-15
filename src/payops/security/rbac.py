"""Roles and permissions. The ONLY place access rules are defined.

The API checks a permission on every endpoint (see api/auth.py). The LLM never
sees these rules and never decides access: it has no tools, no credentials and no
say in which endpoint runs.

Separation of duties: the admin manages accounts and reads the audit log but does
not run investigations; leads can see and resume everyone's investigations.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

Role = Literal["viewer", "analyst", "lead", "admin"]
ROLES: tuple[Role, ...] = ("viewer", "analyst", "lead", "admin")


class Permission(str, Enum):
    ANALYTICS_READ = "analytics:read"                   # dashboards, KPIs, anomalies
    INVESTIGATION_RUN = "investigations:run"
    INVESTIGATION_READ_OWN = "investigations:read_own"
    INVESTIGATION_READ_ALL = "investigations:read_all"
    INVESTIGATION_RESUME_OWN = "investigations:resume_own"
    INVESTIGATION_RESUME_ALL = "investigations:resume_all"
    USERS_MANAGE = "users:manage"
    AUDIT_READ = "audit:read"


P = Permission
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "viewer": frozenset({P.ANALYTICS_READ}),
    "analyst": frozenset({P.ANALYTICS_READ, P.INVESTIGATION_RUN, P.INVESTIGATION_READ_OWN, P.INVESTIGATION_RESUME_OWN}),
    "lead": frozenset({P.ANALYTICS_READ, P.INVESTIGATION_RUN, P.INVESTIGATION_READ_OWN, P.INVESTIGATION_READ_ALL,
                       P.INVESTIGATION_RESUME_OWN, P.INVESTIGATION_RESUME_ALL}),
    "admin": frozenset({P.ANALYTICS_READ, P.INVESTIGATION_READ_ALL, P.USERS_MANAGE, P.AUDIT_READ}),
}


def permissions_for(role: str) -> frozenset[Permission]:
    """Unknown role -> no permissions (fail closed)."""
    return ROLE_PERMISSIONS.get(role, frozenset())


def can(role: str, permission: Permission) -> bool:
    return permission in permissions_for(role)
