"""Where investigations and their step checkpoints are kept.

Two implementations with the same behaviour:
* PostgresStore: the real one, schema "ops", used through the history login only.
* MemoryStore:   for tests (and nothing else), thread-safe like the real one.

Status of an investigation:

    running ──> complete            every step finished
           ──> partial             some steps failed or were skipped (can be resumed)
           ──> interrupted         the process stopped mid-run (Ctrl+C, crash, power cut);
                                   found by the heartbeat check (can be resumed)

Only the workflow runner writes here. Agents never receive the store, and the
history login cannot read payment data.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import Engine, text

from payops.history.codec import as_dict

Status = Literal["running", "complete", "partial", "interrupted"]
RESUMABLE: frozenset[str] = frozenset({"partial", "interrupted"})


class HistoryError(Exception):
    """Base class; messages are safe to show to users."""


class NotFound(HistoryError):
    pass


class NotResumable(HistoryError):
    pass


class InvestigationRecord(BaseModel):
    investigation_id: str
    question: str
    as_of: datetime | None
    use_llm: bool
    status: Status
    mode: Literal["llm", "template"] | None = None
    mode_reason: str | None = None
    headline: str | None = None
    dataset_id: str | None = None
    runs: int = 1
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    result: dict[str, Any] | None = None


class StepRecord(BaseModel):
    step: str
    run: int
    status: Literal["ok", "failed", "skipped"]
    reusable: bool
    payload: dict[str, Any]
    duration_ms: int


class InvestigationStore(ABC):
    @abstractmethod
    def create(self, investigation_id: str, question: str, as_of: datetime | None, use_llm: bool) -> None: ...

    @abstractmethod
    def save_step(self, investigation_id: str, step: str, run: int, status: str, reusable: bool,
                  payload_json: str, duration_ms: int) -> None:
        """Record a step's outcome and refresh the investigation's heartbeat."""

    @abstractmethod
    def finish(self, investigation_id: str, status: Status, mode: str | None, mode_reason: str | None,
               headline: str | None, dataset_id: str | None, duration_ms: int, result_json: str) -> None: ...

    @abstractmethod
    def mark_interrupted(self, investigation_id: str) -> None: ...

    @abstractmethod
    def get(self, investigation_id: str) -> InvestigationRecord:
        """Raises NotFound."""

    @abstractmethod
    def steps(self, investigation_id: str) -> list[StepRecord]: ...

    @abstractmethod
    def list(self, limit: int = 20, offset: int = 0, status: str | None = None) -> list[InvestigationRecord]:
        """Newest first, without the (large) result."""

    @abstractmethod
    def claim_for_resume(self, investigation_id: str) -> InvestigationRecord:
        """Atomically switch a partial/interrupted investigation to running and count the new run.
        Raises NotFound, or NotResumable (complete, or already running elsewhere)."""

    @abstractmethod
    def mark_stale_interrupted(self, older_than_seconds: float) -> int:
        """Running investigations with no heartbeat for this long -> interrupted. Returns how many."""

    @staticmethod
    def _not_resumable(status: str) -> NotResumable:
        if status == "complete":
            return NotResumable("This investigation is complete; there is nothing to resume.")
        return NotResumable("This investigation is already running.")


# ---- PostgreSQL ------------------------------------------------------------------------------

_COLUMNS = ("investigation_id::text AS investigation_id, question, as_of, use_llm, status, mode, mode_reason, headline, "
            "dataset_id, runs, created_at, updated_at, finished_at, duration_ms")


class PostgresStore(InvestigationStore):
    """Every method is one short transaction on the history login's connection pool."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def create(self, investigation_id, question, as_of, use_llm):
        with self._engine.begin() as c:
            c.execute(text("INSERT INTO ops.investigations (investigation_id, question, as_of, use_llm, status) "
                           "VALUES (CAST(:id AS uuid), :q, :as_of, :llm, 'running')"),
                      {"id": investigation_id, "q": question, "as_of": as_of, "llm": use_llm})

    def save_step(self, investigation_id, step, run, status, reusable, payload_json, duration_ms):
        with self._engine.begin() as c:
            c.execute(text("""
                INSERT INTO ops.investigation_steps (investigation_id, step, run, status, reusable, payload, duration_ms)
                VALUES (CAST(:id AS uuid), :step, :run, :status, :reusable, CAST(:payload AS jsonb), :ms)
                ON CONFLICT (investigation_id, step, run) DO UPDATE
                SET status = EXCLUDED.status, reusable = EXCLUDED.reusable, payload = EXCLUDED.payload,
                    duration_ms = EXCLUDED.duration_ms, recorded_at = now()"""),
                      {"id": investigation_id, "step": step, "run": run, "status": status, "reusable": reusable,
                       "payload": payload_json, "ms": duration_ms})
            c.execute(text("UPDATE ops.investigations SET updated_at = now() WHERE investigation_id = CAST(:id AS uuid)"),
                      {"id": investigation_id})

    def finish(self, investigation_id, status, mode, mode_reason, headline, dataset_id, duration_ms, result_json):
        with self._engine.begin() as c:
            c.execute(text("""
                UPDATE ops.investigations
                SET status = :status, mode = :mode, mode_reason = :reason, headline = :headline,
                    dataset_id = coalesce(:dataset_id, dataset_id), duration_ms = :ms,
                    result = CAST(:result AS jsonb), finished_at = now(), updated_at = now()
                WHERE investigation_id = CAST(:id AS uuid)"""),
                      {"id": investigation_id, "status": status, "mode": mode, "reason": mode_reason, "headline": headline,
                       "dataset_id": dataset_id, "ms": duration_ms, "result": result_json})

    def mark_interrupted(self, investigation_id):
        with self._engine.begin() as c:
            c.execute(text("UPDATE ops.investigations SET status = 'interrupted', updated_at = now() "
                           "WHERE investigation_id = CAST(:id AS uuid) AND status = 'running'"), {"id": investigation_id})

    def get(self, investigation_id):
        with self._engine.connect() as c:
            row = c.execute(text(f"SELECT {_COLUMNS}, result FROM ops.investigations "
                                 "WHERE investigation_id = CAST(:id AS uuid)"), {"id": investigation_id}).mappings().first()
        if row is None:
            raise NotFound(f"No investigation with id {investigation_id}.")
        return InvestigationRecord(**{**row, "result": as_dict(row["result"])})

    def steps(self, investigation_id):
        with self._engine.connect() as c:
            rows = c.execute(text("SELECT step, run, status, reusable, payload, duration_ms FROM ops.investigation_steps "
                                  "WHERE investigation_id = CAST(:id AS uuid) ORDER BY run, recorded_at"),
                             {"id": investigation_id}).mappings().all()
        return [StepRecord(**{**r, "payload": as_dict(r["payload"])}) for r in rows]

    def list(self, limit=20, offset=0, status=None):
        with self._engine.connect() as c:
            rows = c.execute(text(f"SELECT {_COLUMNS} FROM ops.investigations "
                                  "WHERE (CAST(:status AS text) IS NULL OR status = :status) "
                                  "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"),
                             {"status": status, "limit": limit, "offset": offset}).mappings().all()
        return [InvestigationRecord(**r) for r in rows]

    def claim_for_resume(self, investigation_id):
        with self._engine.begin() as c:
            row = c.execute(text(f"""
                UPDATE ops.investigations SET status = 'running', runs = runs + 1, updated_at = now(), finished_at = NULL
                WHERE investigation_id = CAST(:id AS uuid) AND status IN ('partial', 'interrupted')
                RETURNING {_COLUMNS}"""), {"id": investigation_id}).mappings().first()
            if row is None:
                current = c.execute(text("SELECT status FROM ops.investigations WHERE investigation_id = CAST(:id AS uuid)"),
                                    {"id": investigation_id}).scalar()
        if row is None:
            if current is None:
                raise NotFound(f"No investigation with id {investigation_id}.")
            raise self._not_resumable(current)
        return InvestigationRecord(**row)

    def mark_stale_interrupted(self, older_than_seconds):
        with self._engine.begin() as c:
            rows = c.execute(text("UPDATE ops.investigations SET status = 'interrupted' "
                                  "WHERE status = 'running' AND updated_at < now() - make_interval(secs => :secs) "
                                  "RETURNING investigation_id"), {"secs": float(older_than_seconds)}).all()
        return len(rows)


# ---- In memory (tests) -------------------------------------------------------------------------

class MemoryStore(InvestigationStore):
    def __init__(self):
        self._lock = threading.Lock()
        self.records: dict[str, InvestigationRecord] = {}
        self.step_rows: dict[str, list[StepRecord]] = {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def create(self, investigation_id, question, as_of, use_llm):
        with self._lock:
            if investigation_id in self.records:
                raise HistoryError("duplicate investigation id")
            now = self._now()
            self.records[investigation_id] = InvestigationRecord(
                investigation_id=investigation_id, question=question, as_of=as_of, use_llm=use_llm, status="running",
                created_at=now, updated_at=now)
            self.step_rows[investigation_id] = []

    def save_step(self, investigation_id, step, run, status, reusable, payload_json, duration_ms):
        row = StepRecord(step=step, run=run, status=status, reusable=reusable, payload=as_dict(payload_json),
                         duration_ms=duration_ms)
        with self._lock:
            rows = self.step_rows[investigation_id]
            rows[:] = [r for r in rows if (r.step, r.run) != (step, run)] + [row]
            self.records[investigation_id].updated_at = self._now()

    def finish(self, investigation_id, status, mode, mode_reason, headline, dataset_id, duration_ms, result_json):
        with self._lock:
            r = self.records[investigation_id]
            now = self._now()
            self.records[investigation_id] = r.model_copy(update=dict(
                status=status, mode=mode, mode_reason=mode_reason, headline=headline,
                dataset_id=dataset_id or r.dataset_id, duration_ms=duration_ms, result=as_dict(result_json),
                finished_at=now, updated_at=now))

    def mark_interrupted(self, investigation_id):
        with self._lock:
            r = self.records.get(investigation_id)
            if r and r.status == "running":
                r.status = "interrupted"

    def get(self, investigation_id):
        with self._lock:
            if investigation_id not in self.records:
                raise NotFound(f"No investigation with id {investigation_id}.")
            return self.records[investigation_id].model_copy(deep=True)

    def steps(self, investigation_id):
        with self._lock:
            return [r.model_copy(deep=True) for r in sorted(self.step_rows.get(investigation_id, []), key=lambda r: r.run)]

    def list(self, limit=20, offset=0, status=None):
        with self._lock:
            rows = sorted((r for r in self.records.values() if status is None or r.status == status),
                          key=lambda r: r.created_at, reverse=True)
            return [r.model_copy(update={"result": None}) for r in rows[offset:offset + limit]]

    def claim_for_resume(self, investigation_id):
        with self._lock:
            r = self.records.get(investigation_id)
            if r is None:
                raise NotFound(f"No investigation with id {investigation_id}.")
            if r.status not in RESUMABLE:
                raise self._not_resumable(r.status)
            r.status, r.runs, r.updated_at, r.finished_at = "running", r.runs + 1, self._now(), None
            return r.model_copy(deep=True)

    def mark_stale_interrupted(self, older_than_seconds):
        cutoff = self._now() - timedelta(seconds=older_than_seconds)
        with self._lock:
            stale = [r for r in self.records.values() if r.status == "running" and r.updated_at < cutoff]
            for r in stale:
                r.status = "interrupted"
            return len(stale)
