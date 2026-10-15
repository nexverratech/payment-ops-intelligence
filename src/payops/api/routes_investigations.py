"""Investigation endpoint.

Phase 4: the question is accepted and validated, and the DETERMINISTIC weekly
review is returned. It is labelled as such. Phase 5 replaces the inside of this
endpoint with the LangGraph agents; the request and response shapes stay.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status

from payops.analytics.report import build_report, render_text
from payops.api.deps import ReadOnlyConn
from payops.api.schemas import InvestigationRequest, InvestigationResponse

router = APIRouter(prefix="/api/v1", tags=["investigations"])
log = logging.getLogger("payops.api.investigations")

PHASE_NOTE = (
    "Deterministic analytics only (no LLM). The question is recorded but not yet interpreted; "
    "the weekly review for the last complete week is returned. Agent-based investigation arrives in Phase 5."
)


@router.post("/investigations", response_model=InvestigationResponse, status_code=status.HTTP_201_CREATED)
def create_investigation(body: InvestigationRequest, conn: ReadOnlyConn) -> InvestigationResponse:
    investigation_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    try:
        report = build_report(conn, body.as_of)
    except ValueError as exc:  # e.g. no previous week to compare against
        raise HTTPException(422, detail=str(exc)) from None
    duration_ms = int((time.perf_counter() - t0) * 1000)
    # The question text is not logged: it may contain whatever a user typed.
    log.info("investigation completed", extra={"fields": {
        "investigation_id": investigation_id, "mode": "deterministic", "duration_ms": duration_ms}})
    return InvestigationResponse(
        investigation_id=investigation_id, mode="deterministic", question=body.question, note=PHASE_NOTE,
        started_at=started_at, duration_ms=duration_ms, report=report, text=render_text(report),
    )
