"""Small HTTP client the dashboard uses to call the local API.

Why the dashboard goes through the API instead of reading the database:
the API is the one place that validates input and (from Phase 7) checks who
is allowed to see what. A second, direct path to the data would bypass that.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx2

from payops.netpolicy import require_local_url


class ApiError(RuntimeError):
    """A readable problem to show on screen (API down, bad filter, server error)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status          # 401 = signed out (expired or revoked session)


@dataclass(frozen=True)
class Filters:
    start: datetime
    end: datetime
    providers: tuple[str, ...] = field(default_factory=tuple)
    methods: tuple[str, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def params(self) -> list[tuple[str, str]]:
        """Query parameters; lists become repeated keys (?provider=A&provider=B)."""
        p = [("start", self.start.isoformat()), ("end", self.end.isoformat())]
        p += [("provider", v) for v in self.providers]
        p += [("method", v) for v in self.methods]
        p += [("reason", v) for v in self.reasons]
        return p


class ApiClient:
    def __init__(self, base_url: str, timeout_seconds: float = 120, transport: httpx2.BaseTransport | None = None,
                 token: str | None = None):
        self.base_url = require_local_url(base_url).rstrip("/")
        self.token = token       # one client per signed-in browser session; never shared between users
        self._http = httpx2.Client(base_url=self.base_url, timeout=timeout_seconds, transport=transport)

    # ---- sign-in ---------------------------------------------------------------
    def login(self, username: str, password: str) -> dict:
        return self._request("POST", "/api/v1/auth/login", json={"username": username, "password": password})

    def logout(self) -> None:
        self._request("POST", "/api/v1/auth/logout", ok_statuses={204}, expect_json=False)

    def me(self) -> dict:
        return self._request("GET", "/api/v1/auth/me")

    # ---- admin -------------------------------------------------------------------
    def audit(self, limit: int = 100, action: str | None = None) -> list:
        params = [("limit", str(limit))] + ([("action", action)] if action else [])
        return self._request("GET", "/api/v1/audit", params=params)

    def verify_audit(self) -> dict:
        return self._request("GET", "/api/v1/audit/verify")

    def users(self) -> list:
        return self._request("GET", "/api/v1/users")

    # ---- endpoints -------------------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/health", ok_statuses={200, 503})

    def meta(self) -> dict:
        return self._request("GET", "/api/v1/meta")

    def kpis(self, f: Filters) -> dict:
        return self._request("GET", "/api/v1/kpis", params=f.params())

    def timeseries(self, f: Filters, unit: str = "day", by_provider: bool = True) -> dict:
        return self._request("GET", "/api/v1/timeseries",
                             params=f.params() + [("unit", unit), ("by_provider", str(by_provider).lower())])

    def breakdown(self, f: Filters, by: str = "provider") -> dict:
        return self._request("GET", "/api/v1/breakdown", params=f.params() + [("by", by)])

    def failure_reasons(self, f: Filters) -> dict:
        return self._request("GET", "/api/v1/failure-reasons", params=f.params())

    def events(self, f: Filters) -> dict:
        return self._request("GET", "/api/v1/events", params=f.params())

    def anomalies(self, f: Filters) -> dict:
        return self._request("GET", "/api/v1/anomalies", params=f.params())

    def investigate(self, question: str, as_of: datetime | None = None, use_llm: bool = True) -> dict:
        body: dict[str, Any] = {"question": question, "use_llm": use_llm}
        if as_of is not None:
            body["as_of"] = as_of.isoformat()
        return self._request("POST", "/api/v1/investigations", json=body, ok_statuses={201})

    def history(self, limit: int = 20, offset: int = 0, status: str | None = None) -> dict:
        params: list[tuple[str, str]] = [("limit", str(limit)), ("offset", str(offset))]
        if status:
            params.append(("status", status))
        return self._request("GET", "/api/v1/investigations", params=params)

    def investigation(self, investigation_id: str) -> dict:
        return self._request("GET", f"/api/v1/investigations/{_uuid(investigation_id)}")

    def resume(self, investigation_id: str) -> dict:
        return self._request("POST", f"/api/v1/investigations/{_uuid(investigation_id)}/resume")

    # ---- plumbing --------------------------------------------------------------
    def _request(self, method: str, path: str, *, params=None, json=None, ok_statuses=frozenset({200}),
                 expect_json: bool = True):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        try:
            response = self._http.request(method, path, params=params, json=json, headers=headers)
        except httpx2.TimeoutException:
            raise ApiError(f"The API at {self.base_url} took too long to answer.") from None
        except httpx2.TransportError:
            raise ApiError(
                f"Cannot reach the API at {self.base_url}. Start it with: "
                "uvicorn payops.api.main:app --host 127.0.0.1 --port 8000"
            ) from None
        if response.status_code == 401:
            raise ApiError(_detail(response) or "Please sign in.", status=401)
        if response.status_code not in ok_statuses:
            raise ApiError(f"API error {response.status_code}: {_detail(response)}", status=response.status_code)
        return response.json() if expect_json else None


def _uuid(value: str) -> str:
    """Only a real UUID goes into a URL path (no path tricks like '../')."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ApiError(f"'{value}' is not an investigation id.") from None


def _detail(response: httpx2.Response) -> str:
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        return response.text[:200]
    if isinstance(detail, list):
        return "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    return str(detail)
