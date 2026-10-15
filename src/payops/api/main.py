"""FastAPI application entry point.

Run with:  uvicorn payops.api.main:app --app-dir src --host 127.0.0.1 --port 8000

Phase 1 exposes only GET /health. Later phases add investigation endpoints.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import Depends, FastAPI, Response, status
from pydantic import BaseModel

from payops import __version__, health
from payops.config import Settings, get_settings
from payops.logging_setup import configure_logging

log = logging.getLogger("payops.api")


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    version: str
    environment: str
    llm_mode: Literal["ollama", "fallback"]
    components: dict[str, health.ComponentHealth]


def create_app() -> FastAPI:
    configure_logging(get_settings().log_level)
    app = FastAPI(
        title="Payment Operations Intelligence API",
        version=__version__,
        # The interactive /docs page loads its JavaScript from a public CDN by default.
        # We switch it off so the app makes no outbound requests (see docs/offline_checklist.md).
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/health", response_model=HealthResponse)
    def get_health(response: Response, settings: Settings = Depends(get_settings)) -> HealthResponse:
        db = health.check_database(settings)
        llm = health.check_llm(settings)

        # The database is essential. The LLM is not: without it the app runs
        # in clearly labelled fallback mode, so it is "degraded", not "down".
        if db.status != "ok":
            overall = "down"
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        elif llm.status != "ok":
            overall = "degraded"
        else:
            overall = "ok"

        result = HealthResponse(
            status=overall,
            version=__version__,
            environment=settings.app_env,
            llm_mode="ollama" if llm.status == "ok" else "fallback",
            components={"database": db, "llm": llm},
        )
        log.info(
            "health check",
            extra={"fields": {"status": overall, "database": db.status, "llm": llm.status}},
        )
        return result

    return app


app = create_app()
