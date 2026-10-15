"""Shared FastAPI dependencies: database connection and validated filters.

A "dependency" is a function FastAPI runs before an endpoint and passes in
its result. Putting connection and validation logic here means every endpoint
gets them the same way, and tests can swap them out.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy.engine import Connection

from payops.analytics import queries
from payops.analytics.metrics import last_complete_week
from payops.analytics.models import AnalysisFilter
from payops.config import Settings, get_settings
from payops.db.connection import get_readonly_engine


def get_readonly_conn(settings: Annotated[Settings, Depends(get_settings)]) -> Iterator[Connection]:
    """A read-only database connection for one request, returned to the pool afterwards."""
    with get_readonly_engine(settings).connect() as conn:
        yield conn


ReadOnlyConn = Annotated[Connection, Depends(get_readonly_conn)]


def analysis_filter(
    conn: ReadOnlyConn,
    start: Annotated[datetime | None, Query(description="UTC start, inclusive (ISO 8601 with timezone)")] = None,
    end: Annotated[datetime | None, Query(description="UTC end, exclusive")] = None,
    provider: Annotated[list[str] | None, Query(description="Provider code(s), e.g. KESTREL")] = None,
    method: Annotated[list[str] | None, Query(description="Payment method(s), e.g. CARD")] = None,
    reason: Annotated[list[str] | None, Query(description="Count only failures with these reason(s)")] = None,
) -> AnalysisFilter:
    """Build and validate the filter. Without start/end: the last complete week of the loaded data."""
    if start is None or end is None:
        default_start, default_end = last_complete_week(queries.dataset_window(conn)["window_end"])
        start, end = start or default_start, end or default_end
    try:
        return AnalysisFilter(
            start=start, end=end,
            provider_codes=tuple(provider or ()), payment_methods=tuple(method or ()), failure_reasons=tuple(reason or ()),
        )
    except ValidationError as exc:
        raise HTTPException(422, detail=validation_messages(exc)) from None


Filter = Annotated[AnalysisFilter, Depends(analysis_filter)]


def validation_messages(exc: ValidationError) -> list[str]:
    """Short, readable messages without echoing whole inputs back."""
    return [f"{'.'.join(str(p) for p in e['loc']) or 'request'}: {e['msg']}" for e in exc.errors(include_url=False)]
