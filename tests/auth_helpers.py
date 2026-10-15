"""Test helper: act as a signed-in user without going through the login endpoint.

Overrides the two FastAPI dependencies the API uses for sign-in. The permission checks
themselves (`require(...)`, ownership) still run for real, so tests exercise the same
access rules as production. Login itself is tested in test_api_auth.py.
"""

from payops.api.auth import current_user, get_auth_store
from payops.security.rbac import permissions_for
from payops.security.service import Principal
from payops.security.store import MemoryAuthStore


def sign_in_as(app, role: str = "lead", username: str = "tester", store: MemoryAuthStore | None = None) -> MemoryAuthStore:
    store = store or MemoryAuthStore()
    app.dependency_overrides[get_auth_store] = lambda: store
    app.dependency_overrides[current_user] = lambda: Principal(username, role, permissions_for(role))
    return store
