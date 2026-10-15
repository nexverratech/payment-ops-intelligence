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
    def __init__(self, base_url: str, timeout_seconds: float = 120, transport: httpx2.BaseTransport | None = None):
        self.base_url = require_local_url(base_url).rstrip("/")
        self._http = httpx2.Client(base_url=self.base_url, timeout=timeout_seconds, transport=transport)

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
    def _request(self, method: str, path: str, *, params=None, json=None, ok_statuses=frozenset({200})) -> dict:
        try:
            response = self._http.request(method, path, params=params, json=json)
        except httpx2.TimeoutException:
            raise ApiError(f"The API at {self.base_url} took too long to answer.") from None
        except httpx2.TransportError:
            raise ApiError(
                f"Cannot reach the API at {self.base_url}. Start it with: "
                "uvicorn payops.api.main:app --host 127.0.0.1 --port 8000"
            ) from None
        if response.status_code not in ok_statuses:
            raise ApiError(f"API error {response.status_code}: {_detail(response)}")
        return response.json()


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
