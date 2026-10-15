"""SQL queries for analytics. Always run on the READ-ONLY connection.

Safety rules followed here:
* Every value from a user or agent is a bound parameter (:start, :providers, ...).
  It is sent separately from the SQL text, so it can never change the query.
* Things that can't be parameters (column names, time units) are chosen from
  fixed dictionaries in this file, never built from input.
* Queries return small aggregated tables; the heavy lifting stays in PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import pandas as pd
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from payops.analytics.models import AnalysisFilter

# Allowed groupings -> SQL column. Input picks a KEY; the SQL comes from here.
DIMENSIONS: dict[str, str] = {
    "provider": "provider_code",
    "method": "payment_method",
}
TIME_UNITS: dict[str, str] = {"hour": "hour", "day": "day"}


def _filter_sql(f: AnalysisFilter) -> tuple[str, str, dict, list]:
    """Returns (WHERE clause, failure condition, parameters, expanding bind params)."""
    where = ["created_at >= :start", "created_at < :end"]
    params: dict = {"start": f.start, "end": f.end}
    binds = []
    for values, column, name in (
        (f.provider_codes, "provider_code", "providers"),
        (f.payment_methods, "payment_method", "methods"),
    ):
        if values:
            where.append(f"{column} IN :{name}")
            params[name] = list(values)
            binds.append(bindparam(name, expanding=True))
    failed = "status = 'FAILED'"
    if f.failure_reasons:
        failed += " AND failure_reason IN :reasons"
        params["reasons"] = list(f.failure_reasons)
        binds.append(bindparam("reasons", expanding=True))
    return " AND ".join(where), failed, params, binds


def _frame(conn: Connection, sql: str, params: dict, binds: list) -> pd.DataFrame:
    stmt = text(sql).bindparams(*binds) if binds else text(sql)
    rows = conn.execute(stmt, params).mappings().all()
    df = pd.DataFrame([dict(r) for r in rows])
    for col in ("total_value", "failed_value"):
        if col in df:
            df[col] = df[col].astype(float)  # NUMERIC arrives as Decimal
    return df


def dataset_window(conn: Connection) -> dict:
    """The loaded dataset: id, first and last moment covered."""
    row = conn.execute(text("SELECT dataset_id, window_start, window_end FROM payments.dataset_runs")).mappings().one()
    return dict(row)


def summary(conn: Connection, f: AnalysisFilter) -> dict:
    where, failed, params, binds = _filter_sql(f)
    df = _frame(conn, f"""
        SELECT count(*)                                           AS attempts,
               count(*) FILTER (WHERE {failed})                   AS failures,
               coalesce(sum(amount), 0)                           AS total_value,
               coalesce(sum(amount) FILTER (WHERE {failed}), 0)   AS failed_value
        FROM payments.transactions
        WHERE {where}""", params, binds)
    return df.iloc[0].to_dict()


def breakdown(conn: Connection, f: AnalysisFilter, by: Literal["provider", "method"]) -> pd.DataFrame:
    """Attempts, failures and values per provider or per payment method."""
    column = DIMENSIONS[by]  # KeyError for anything not allowed
    where, failed, params, binds = _filter_sql(f)
    return _frame(conn, f"""
        SELECT {column}                                           AS {column},
               count(*)                                           AS attempts,
               count(*) FILTER (WHERE {failed})                   AS failures,
               sum(amount)                                        AS total_value,
               coalesce(sum(amount) FILTER (WHERE {failed}), 0)   AS failed_value
        FROM payments.transactions
        WHERE {where}
        GROUP BY {column}
        ORDER BY {column}""", params, binds)


def failure_reasons(conn: Connection, f: AnalysisFilter) -> pd.DataFrame:
    """Failures and failed value per failure reason."""
    where, failed, params, binds = _filter_sql(f)
    return _frame(conn, f"""
        SELECT failure_reason, count(*) AS failures, sum(amount) AS failed_value
        FROM payments.transactions
        WHERE {where} AND {failed}
        GROUP BY failure_reason
        ORDER BY failures DESC, failure_reason""", params, binds)


def time_series(conn: Connection, f: AnalysisFilter, unit: Literal["hour", "day"], by_provider: bool) -> pd.DataFrame:
    """Attempts and failures per hour or day, optionally per provider.

    The time column is named after the unit ('hour' or 'day') and is UTC.
    """
    trunc = TIME_UNITS[unit]
    where, failed, params, binds = _filter_sql(f)
    group = "provider_code, " if by_provider else ""
    df = _frame(conn, f"""
        SELECT {group}date_trunc('{trunc}', created_at)               AS {trunc},
               count(*)                                           AS attempts,
               count(*) FILTER (WHERE {failed})                   AS failures,
               coalesce(sum(amount) FILTER (WHERE {failed}), 0)   AS failed_value
        FROM payments.transactions
        WHERE {where}
        GROUP BY {group}{trunc}
        ORDER BY {group}{trunc}""", params, binds)
    if not df.empty:
        df[trunc] = pd.to_datetime(df[trunc], utc=True)
    return df


def events(conn: Connection, start: datetime, end: datetime) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Deployments and provider notices that touch [start, end)."""
    params = {"start": start, "end": end}
    deployments = _frame(conn, """
        SELECT deployment_id, service, version, deployed_at, team, change_summary
        FROM payments.deployments
        WHERE deployed_at >= :start AND deployed_at < :end
        ORDER BY deployed_at""", params, [])
    notices = _frame(conn, """
        SELECT incident_id, provider_code, reported_start_at, reported_end_at, severity, title
        FROM payments.provider_incidents
        WHERE reported_start_at < :end AND coalesce(reported_end_at, 'infinity') > :start
        ORDER BY reported_start_at""", params, [])
    return deployments, notices


def sample_failed_transactions(conn: Connection, f: AnalysisFilter, limit: int = 5) -> list[str]:
    """A few transaction IDs that support a finding (evidence references, not customer data)."""
    where, failed, params, binds = _filter_sql(f)
    params["limit"] = max(1, min(int(limit), 50))
    df = _frame(conn, f"""
        SELECT transaction_id FROM payments.transactions
        WHERE {where} AND {failed}
        ORDER BY created_at, transaction_id
        LIMIT :limit""", params, binds)
    return df["transaction_id"].tolist() if not df.empty else []
