"""Investigation endpoint: runs the multi-agent workflow.

POST /api/v1/investigations  {"question": "...", "as_of": null, "use_llm": true}

* A fresh LLM client is created per investigation, so each one gets its own
  call budget (LLM_MAX_CALLS).
* The workflow never crashes the request: agent failures give status "partial",
  an unavailable LLM gives the clearly labelled template report.
* The question text is not logged, because it may contain anything a user typed.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, status

from payops.agents.graph import run_investigation
from payops.api.schemas import InvestigationRequest, InvestigationResponse
from payops.config import Settings, get_settings
from payops.db.connection import get_readonly_engine
from payops.llm.client import OllamaClient

router = APIRouter(prefix="/api/v1", tags=["investigations"])
log = logging.getLogger("payops.api.investigations")


@router.post("/investigations", response_model=InvestigationResponse, status_code=status.HTTP_201_CREATED)
def create_investigation(body: InvestigationRequest,
                         settings: Annotated[Settings, Depends(get_settings)]) -> InvestigationResponse:
    investigation_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    llm = OllamaClient.from_settings(settings) if body.use_llm else None
    result = run_investigation(get_readonly_engine(settings), body.question, body.as_of, investigation_id, llm)
    log.info("investigation completed", extra={"fields": {
        "investigation_id": investigation_id, "status": result["status"], "mode": result["mode"],
        "duration_ms": result["duration_ms"], "llm_calls": len(result.get("llm_calls", []))}})
    return InvestigationResponse(
        investigation_id=investigation_id, status=result["status"], mode=result["mode"], mode_reason=result["mode_reason"],
        question=body.question, started_at=started_at, duration_ms=result["duration_ms"], report=result,
        text=result["report_text"],
    )
