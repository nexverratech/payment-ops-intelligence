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

from payops.config import Settings
from payops.db.connection import get_engine

log = logging.getLogger(__name__)

Status = Literal["ok", "unreachable", "model_missing", "error"]


class ComponentHealth(BaseModel):
    status: Status
    latency_ms: float | None = None
    detail: str | None = None


def check_database(settings: Settings) -> ComponentHealth:
    """Run `SELECT 1` against PostgreSQL."""
    started = time.perf_counter()
    try:
        with get_engine(settings).connect() as conn:
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
