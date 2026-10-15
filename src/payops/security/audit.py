"""The audit log: who did what, when, and with what outcome.

Two protections, in layers:
1. Append-only in the database. The application login may only INSERT and SELECT,
   and a trigger rejects UPDATE, DELETE and TRUNCATE even for the owner
   (security_schema.sql). Changing history needs a deliberate DBA action.
2. A hash chain. Each row stores the hash of the previous row, and its own hash covers
   its content plus that previous hash. Editing or deleting a row in the middle
   breaks every hash after it, and `verify_chain` reports the first broken row.
   Limitation: deleting the newest rows leaves a shorter chain that still checks out.
   Keep the latest hash and count (printed by `python -m payops.security.manage
   verify-audit`) somewhere else if that matters.

What is NOT written to the audit log: passwords, tokens, question text, report text.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

GENESIS = "0" * 64
Outcome = Literal["success", "denied", "failure"]
Source = Literal["api", "cli"]
DetailValue = str | int | bool | None     # no floats: they must survive a JSONB round trip unchanged


class AuditEvent(BaseModel):
    actor: str = Field(max_length=80)              # username, "cli:<os user>", or "anonymous"
    role: str | None = Field(default=None, max_length=20)
    action: str = Field(pattern=r"^[a-z_]+\.[a-z_]+$", max_length=60)    # e.g. "auth.login", "investigation.run"
    target: str | None = Field(default=None, max_length=200)
    outcome: Outcome
    source: Source = "api"
    details: dict[str, DetailValue] = Field(default_factory=dict)

    @field_validator("details")
    @classmethod
    def _small(cls, value: dict) -> dict:
        if len(value) > 20 or any(isinstance(v, str) and len(v) > 300 for v in value.values()):
            raise ValueError("audit details must be small (at most 20 keys, 300 characters each)")
        return value


class AuditRecord(AuditEvent):
    id: int
    at: datetime
    prev_hash: str
    hash: str


def chain_hash(prev_hash: str, at: datetime, event: AuditEvent) -> str:
    body = {"at": at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), **event.model_dump(mode="json")}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(f"{prev_hash}\n{canonical}".encode("utf-8")).hexdigest()


class ChainReport(BaseModel):
    ok: bool
    rows: int
    first_bad_id: int | None = None
    problem: str | None = None
    last_hash: str


def verify_chain(records: Iterable[AuditRecord]) -> ChainReport:
    """Walk the log oldest first and recompute every hash."""
    prev, n = GENESIS, 0
    for r in records:
        n += 1
        if r.prev_hash != prev:
            return ChainReport(ok=False, rows=n, first_bad_id=r.id, last_hash=prev,
                               problem="previous-hash link broken (a row before it was removed or changed)")
        if chain_hash(r.prev_hash, r.at, AuditEvent(**r.model_dump(include=set(AuditEvent.model_fields)))) != r.hash:
            return ChainReport(ok=False, rows=n, first_bad_id=r.id, last_hash=prev,
                               problem="content does not match its hash (the row was changed)")
        prev = r.hash
    return ChainReport(ok=True, rows=n, last_hash=prev)
