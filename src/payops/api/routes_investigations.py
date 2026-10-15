"""Investigation endpoints: run, list, open and resume.

POST /api/v1/investigations                 {"question": "...", "as_of": null, "use_llm": true}
GET  /api/v1/investigations?limit=20&offset=0&status=partial
GET  /api/v1/investigations/{id}
POST /api/v1/investigations/{id}/resume

* A fresh LLM client is created per run, so each one gets its own call budget.
* The workflow never crashes the request: agent failures give status "partial",
  an unavailable LLM gives the clearly labelled template report.
* Every run is saved to history when APP_DB_PASSWORD is configured. Without it,
  investigations still work; listing and resuming answer 503 with an explanation.
* At most MAX_CONCURRENT_INVESTIGATIONS run at once (they share one local GPU);
  more get 429 "busy, try again".
* The question text is not logged, because it may contain anything a user typed.

Access (Phase 7, enforced here, never by the LLM):
* run:     investigations:run (analyst, lead)
* see:     your own with investigations:read_own; everyone's with investigations:read_all (lead, admin)
* resume:  your own with investigations:resume_own; anyone's with investigations:resume_all (lead)
* Someone else's investigation you may not see answers 404, not 403, so ids cannot be probed.
* Every run, view and resume is written to the audit log (without the question text).
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from payops.agents.graph import resume_investigation, run_investigation
from payops.api.auth import AuthStoreDep, audit, deny, require
from payops.api.schemas import HistoryItem, HistoryList, InvestigationRequest, InvestigationResponse, SavedInvestigation
from payops.config import Settings, get_settings
from payops.db.connection import get_history_engine, get_readonly_engine
from payops.history.store import RESUMABLE, InvestigationRecord, InvestigationStore, NotFound, NotResumable, PostgresStore
from payops.llm.client import OllamaClient
from payops.security.rbac import Permission as P
from payops.security.service import Principal

router = APIRouter(prefix="/api/v1", tags=["investigations"])
log = logging.getLogger("payops.api.investigations")

InvestigationId = Annotated[uuid.UUID, Path(description="The investigation's id (a UUID)")]


# ---- dependencies --------------------------------------------------------------------------

def get_store(settings: Annotated[Settings, Depends(get_settings)]) -> InvestigationStore | None:
    """The history store, or None when APP_DB_PASSWORD is not configured."""
    return PostgresStore(get_history_engine(settings)) if settings.history_enabled else None


Store = Annotated[InvestigationStore | None, Depends(get_store)]
Config = Annotated[Settings, Depends(get_settings)]


def _require(store: InvestigationStore | None) -> InvestigationStore:
    if store is None:
        raise HTTPException(503, detail="Investigation history is not configured: set APP_DB_PASSWORD in .env "
                                        "and run: python -m payops.db.setup_history")
    return store


class _Slots:
    """How many investigations may run at the same moment (sized from settings on first use)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sem: threading.BoundedSemaphore | None = None
        self.size = 0

    def acquire(self, size: int) -> bool:
        with self._lock:
            if self._sem is None or self.size != size:
                self._sem, self.size = threading.BoundedSemaphore(size), size
            sem = self._sem
        return sem.acquire(blocking=False)

    def release(self) -> None:
        if self._sem is not None:
            self._sem.release()


SLOTS = _Slots()


def _run_limited(settings: Settings, work: Callable[[], dict]) -> dict:
    if not SLOTS.acquire(settings.max_concurrent_investigations):
        raise HTTPException(429, detail=f"{settings.max_concurrent_investigations} investigations are already running; "
                                        "try again when one finishes.")
    try:
        return work()
    finally:
        SLOTS.release()


def _llm_factory(settings: Settings) -> Callable[[], OllamaClient]:
    return lambda: OllamaClient.from_settings(settings)


def _response(result: dict, question: str, started_at: datetime) -> InvestigationResponse:
    log.info("investigation completed", extra={"fields": {
        "investigation_id": result["investigation_id"], "status": result["status"], "mode": result["mode"],
        "duration_ms": result["duration_ms"], "llm_calls": len(result.get("llm_calls", [])),
        "run": result.get("run", 1), "persisted": result.get("persisted", False)}})
    return InvestigationResponse(
        investigation_id=result["investigation_id"], status=result["status"], mode=result["mode"],
        mode_reason=result["mode_reason"], question=question, started_at=started_at, duration_ms=result["duration_ms"],
        report=result, text=result["report_text"], persisted=result.get("persisted", False), run=result.get("run", 1),
        reused_steps=result.get("reused_steps", []))


def _item(r: InvestigationRecord) -> dict:
    return {**r.model_dump(exclude={"result", "as_of", "mode_reason", "updated_at"}), "resumable": r.status in RESUMABLE}


# ---- endpoints -----------------------------------------------------------------------------

Runner = Annotated[Principal, Depends(require(P.INVESTIGATION_RUN))]
Reader = Annotated[Principal, Depends(require(P.INVESTIGATION_READ_OWN, P.INVESTIGATION_READ_ALL))]
Resumer = Annotated[Principal, Depends(require(P.INVESTIGATION_RESUME_OWN, P.INVESTIGATION_RESUME_ALL))]


def _visible(user: Principal, r: InvestigationRecord) -> bool:
    return user.can(P.INVESTIGATION_READ_ALL) or (user.can(P.INVESTIGATION_READ_OWN) and r.created_by == user.username)


def _load(store: InvestigationStore, user: Principal, investigation_id: str) -> InvestigationRecord:
    """The record if this user may see it; otherwise 404 (whether or not it exists)."""
    try:
        r = store.get(investigation_id)
    except NotFound:
        r = None
    if r is None or not _visible(user, r):
        raise HTTPException(404, detail=f"No investigation with id {investigation_id}.")
    return r


@router.post("/investigations", response_model=InvestigationResponse, status_code=status.HTTP_201_CREATED)
def create_investigation(body: InvestigationRequest, user: Runner, settings: Config, store: Store,
                         auth: AuthStoreDep) -> InvestigationResponse:
    investigation_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    audit(auth, user, "investigation.start", target=investigation_id, details={"use_llm": body.use_llm})
    llm = OllamaClient.from_settings(settings) if body.use_llm else None
    result = _run_limited(settings, lambda: run_investigation(
        get_readonly_engine(settings), body.question, body.as_of, investigation_id, llm,
        store=store, timeout_seconds=settings.investigation_timeout_seconds, requested_by=user.username))
    audit(auth, user, "investigation.finish", target=investigation_id,
          outcome="success" if result["status"] == "complete" else "failure",
          details={"status": result["status"], "mode": result["mode"], "persisted": result.get("persisted", False)})
    return _response(result, body.question, started_at)


@router.get("/investigations", response_model=HistoryList)
def list_investigations(
    user: Reader, settings: Config, store: Store,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    status_filter: Annotated[Literal["running", "complete", "partial", "interrupted"] | None,
                             Query(alias="status")] = None,
) -> HistoryList:
    store = _require(store)
    store.mark_stale_interrupted(settings.stale_after_seconds)
    only = None if user.can(P.INVESTIGATION_READ_ALL) else user.username
    rows = store.list(limit=limit, offset=offset, status=status_filter, created_by=only)
    return HistoryList(items=[HistoryItem(**_item(r)) for r in rows], limit=limit, offset=offset)


@router.get("/investigations/{investigation_id}", response_model=SavedInvestigation)
def get_investigation(investigation_id: InvestigationId, user: Reader, settings: Config, store: Store,
                      auth: AuthStoreDep) -> SavedInvestigation:
    store = _require(store)
    store.mark_stale_interrupted(settings.stale_after_seconds)
    r = _load(store, user, str(investigation_id))
    audit(auth, user, "investigation.view", target=r.investigation_id)
    return SavedInvestigation(**_item(r), as_of=r.as_of, mode_reason=r.mode_reason, result=r.result)


@router.post("/investigations/{investigation_id}/resume", response_model=InvestigationResponse)
def resume(investigation_id: InvestigationId, request: Request, user: Resumer, settings: Config, store: Store,
           auth: AuthStoreDep) -> InvestigationResponse:
    store = _require(store)
    store.mark_stale_interrupted(settings.stale_after_seconds)
    r = _load(store, user, str(investigation_id))
    if not (user.can(P.INVESTIGATION_RESUME_ALL) or r.created_by == user.username):
        raise deny(auth, user, request, P.INVESTIGATION_RESUME_ALL.value)
    started_at = datetime.now(timezone.utc)
    try:
        result = _run_limited(settings, lambda: resume_investigation(
            get_readonly_engine(settings), store, str(investigation_id), _llm_factory(settings),
            timeout_seconds=settings.investigation_timeout_seconds))
    except NotResumable as exc:
        audit(auth, user, "investigation.resume", target=r.investigation_id, outcome="failure",
              details={"reason": str(exc)[:200]})
        raise HTTPException(409, detail=str(exc)) from None
    audit(auth, user, "investigation.resume", target=r.investigation_id,
          outcome="success" if result["status"] == "complete" else "failure",
          details={"status": result["status"], "run": result.get("run", 1)})
    return _response(result, result["question"], started_at)
