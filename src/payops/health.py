"""Health checks for the components the app depends on.

Each check returns a ComponentHealth and never raises: the /health endpoint
must always answer, even when the thing it is checking is broken.
Error details are reduced to the exception type plus a hint, so no
connection strings or passwords can leak into an API response.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

import httpx2
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from payops.config import Settings
from payops.db.connection import get_history_engine, get_readonly_engine

log = logging.getLogger(__name__)

Status = Literal["ok", "unreachable", "model_missing", "error", "not_configured", "not_set_up"]


class ComponentHealth(BaseModel):
    status: Status
    latency_ms: float | None = None
    detail: str | None = None


def check_database(settings: Settings) -> ComponentHealth:
    """Run `SELECT 1` against PostgreSQL with the read-only login: the login the API really uses,
    so the API process never needs the owner password (least privilege, Phase 8)."""
    started = time.perf_counter()
    if settings.readonly_db_password is None:
        return ComponentHealth(status="not_configured", detail="READONLY_DB_PASSWORD is not set")
    try:
        with get_readonly_engine(settings).connect() as conn:
            conn.execute(text("SELECT 1")).scalar_one()
        return ComponentHealth(status="ok", latency_ms=_ms_since(started))
    except Exception as exc:  # noqa: BLE001 - any failure means "not healthy"
        log.warning("database health check failed", extra={"fields": {"error_type": type(exc).__name__}})
        return ComponentHealth(
            status="unreachable",
            latency_ms=_ms_since(started),
            detail=f"{type(exc).__name__}. Is the container running? Try: docker compose ps",
        )


def check_llm(settings: Settings) -> ComponentHealth:
    """Ask the local Ollama server which models are installed (GET /api/tags)."""
    started = time.perf_counter()
    try:
        response = httpx2.get(
            f"{settings.ollama_base_url}/api/tags",
            timeout=settings.health_check_timeout_seconds,
        )
        response.raise_for_status()
        installed = {m.get("name") for m in response.json().get("models", [])}
    except Exception as exc:  # noqa: BLE001
        log.info("ollama not reachable; fallback mode", extra={"fields": {"error_type": type(exc).__name__}})
        return ComponentHealth(
            status="unreachable",
            latency_ms=_ms_since(started),
            detail=f"{type(exc).__name__}. Start Ollama, or continue in fallback mode.",
        )

    if settings.ollama_model not in installed:
        return ComponentHealth(
            status="model_missing",
            latency_ms=_ms_since(started),
            detail=f"Model '{settings.ollama_model}' is not pulled. Run: ollama pull {settings.ollama_model}",
        )
    return ComponentHealth(status="ok", latency_ms=_ms_since(started))


def _ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def check_history(settings: Settings) -> ComponentHealth:
    """Can the history login reach its tables? (Optional component: see setup_history.py.)"""
    if not settings.history_enabled:
        return ComponentHealth(status="not_configured", detail="APP_DB_PASSWORD not set: investigations are not saved")
    started = time.perf_counter()
    try:
        with get_history_engine(settings).connect() as conn:
            conn.execute(text("SELECT 1 FROM ops.investigations LIMIT 1")).all()
        return ComponentHealth(status="ok", latency_ms=_ms_since(started))
    except ProgrammingError as exc:   # missing table or missing permission
        log.warning("history not set up", extra={"fields": {"error_type": type(exc.orig).__name__}})
        return ComponentHealth(status="not_set_up", latency_ms=_ms_since(started),
                               detail="History tables missing or not permitted. Run: python -m payops.db.setup_history")
    except Exception as exc:  # noqa: BLE001
        log.warning("history health check failed", extra={"fields": {"error_type": type(exc).__name__}})
        return ComponentHealth(status="unreachable", latency_ms=_ms_since(started),
                               detail=f"{type(exc).__name__}. Is the container running? Is APP_DB_PASSWORD correct?")
