"""Turning workflow data into stored JSON and back, safely.

Why not pickle (or LangGraph's msgpack checkpoints)? Loading a pickle can run
arbitrary code, so anyone able to edit the checkpoint table could take over the
app when a run is resumed. LangGraph's own checkpoint loader had exactly this
class of problem (CVE-2026-28277, fixed in langgraph 1.0.10).

Here a checkpoint is plain JSON, and loading it means *validating* it against
InvestigationState: only the fields and types declared there can come back
(strings, numbers, dates and our own Pydantic models). Unknown fields are dropped.
Nothing in the stored data can choose which Python class gets created.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import TypeAdapter

from payops.agents.state import InvestigationState

_STATE = TypeAdapter(InvestigationState)
_JSON = TypeAdapter(dict[str, Any])


def encode_update(update: dict[str, Any]) -> str:
    """A step's state update -> JSON text (NaN and infinity become null, which PostgreSQL accepts)."""
    return _STATE.dump_json(update).decode("utf-8")


def decode_update(payload: str | bytes | dict[str, Any]) -> dict[str, Any]:
    """Stored JSON -> validated state update with our real types (datetimes, Pydantic models)."""
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    return dict(_STATE.validate_json(payload))


def encode_json(value: dict[str, Any]) -> str:
    """Any JSON-like dict (e.g. the final result) -> JSON text, NaN -> null."""
    return _JSON.dump_json(value).decode("utf-8")


def as_dict(value: Any) -> dict[str, Any] | None:
    """A JSONB column value: the driver returns a dict; tolerate text as well."""
    if value is None or isinstance(value, dict):
        return value
    return json.loads(value)
