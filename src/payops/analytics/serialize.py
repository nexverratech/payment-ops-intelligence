"""Turn analytics tables into JSON-friendly data for the API and reports."""

from __future__ import annotations

import json
import math

import pandas as pd


def to_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> list of plain dicts: NaN becomes None, numpy types become Python types,
    timestamps become ISO-8601 strings."""
    if df is None or df.empty:
        return []
    rows = json.loads(df.to_json(orient="records", date_format="iso"))
    return [{k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in row.items()} for row in rows]
