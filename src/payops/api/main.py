"""FastAPI application entry point.

Run with:  uvicorn payops.api.main:app --host 127.0.0.1 --port 8000

Endpoints
* GET  /health                      component status
* GET  /api/v1/meta                 loaded dataset and filter values
* GET  /api/v1/kpis                 headline numbers vs the previous period
* GET  /api/v1/timeseries           attempts / failures / rate per hour or day
* GET  /api/v1/breakdown            per provider or method, vs the previous period
* GET  /api/v1/failure-reasons      failures per reason, vs the previous period
* GET  /api/v1/events               deployments and provider notices
* GET  /api/v1/anomalies            detected anomaly windows + related events
* POST /api/v1/investigations       run an investigation (saved to history when configured)
* GET  /api/v1/investigations       saved investigations, newest first
* GET  /api/v1/investigations/{id}  one saved investigation with its full result
* POST /api/v1/investigations/{id}/resume   continue a partial or interrupted one from its checkpoints

* POST /api/v1/auth/login, /auth/logout, GET /auth/me, POST /auth/password      sign-in (Phase 7)
* /api/v1/users..., /api/v1/audit...                                            admin only

Every route except /health and /api/v1/auth/login requires a signed-in user with the
right permission (api/auth.py; a test checks that no route is left unprotected).
The API listens on 127.0.0.1 only.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import DBAPIError, OperationalError

from payops import __version__, health
from payops.api.routes_analytics import router as analytics_router
from payops.api.routes_auth import router as auth_router
from payops.api.routes_investigations import router as investigations_router
from payops.config import Settings, get_settings
from payops.logging_setup import configure_logging

log = logging.getLogger("payops.api")


class _TooLarge(HTTPException):
    """An HTTPException, so FastAPI passes it through as 413 instead of turning it into a 400."""

    def __init__(self, max_bytes: int):
        super().__init__(413, detail=f"Request body larger than {max_bytes} bytes.")


class BodySizeLimit:
    """Refuse request bodies over max_bytes with 413, whether or not Content-Length is sent."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > self.max_bytes):
            return await _too_large(scope, receive, send, self.max_bytes)
        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _TooLarge(self.max_bytes)
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _TooLarge:
            await _too_large(scope, receive, send, self.max_bytes)


async def _too_large(scope, receive, send, max_bytes: int) -> None:
    await JSONResponse(status_code=413, content={"detail": f"Request body larger than {max_bytes} bytes."})(
        scope, receive, send)


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
    app.include_router(auth_router)
    app.include_router(analytics_router)
    app.include_router(investigations_router)
    app.add_middleware(BodySizeLimit, max_bytes=get_settings().max_request_bytes)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"      # responses hold payment data and tokens
        return response

    # ---- Error handling: clear status codes, no internals in responses ----------
    @app.exception_handler(OperationalError)
    async def database_unavailable(request: Request, exc: OperationalError) -> JSONResponse:
        log.warning("database unavailable", extra={"fields": {"path": request.url.path}})
        return JSONResponse(status_code=503, content={"detail": "Database unavailable. Is the container running?"})

    @app.exception_handler(DBAPIError)
    async def database_error(request: Request, exc: DBAPIError) -> JSONResponse:
        log.error("database error", extra={"fields": {"path": request.url.path, "error_type": type(exc.orig).__name__}})
        return JSONResponse(status_code=500, content={"detail": "Database query failed."})

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error", extra={"fields": {"path": request.url.path, "error_type": type(exc).__name__}})
        return JSONResponse(status_code=500, content={"detail": "Internal error."})

    @app.get("/health", response_model=HealthResponse)
    def get_health(response: Response, settings: Settings = Depends(get_settings)) -> HealthResponse:
        db = health.check_database(settings)
        llm = health.check_llm(settings)
        history = health.check_history(settings)

        # The database is essential. The LLM is not: without it the app runs
        # in clearly labelled fallback mode, so it is "degraded", not "down".
        # History is not essential either: without it investigations run but are not saved.
        if db.status != "ok":
            overall = "down"
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        elif llm.status != "ok" or history.status not in {"ok", "not_configured"}:
            overall = "degraded"
        else:
            overall = "ok"

        result = HealthResponse(
            status=overall,
            version=__version__,
            environment=settings.app_env,
            llm_mode="ollama" if llm.status == "ok" else "fallback",
            components={"database": db, "llm": llm, "history": history},
        )
        log.info(
            "health check",
            extra={"fields": {"status": overall, "database": db.status, "llm": llm.status}},
        )
        return result

    return app


app = create_app()
