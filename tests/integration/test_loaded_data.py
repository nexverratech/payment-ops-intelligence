"""Checks the dataset that is loaded in PostgreSQL. Skipped unless both the
container is running AND `python -m payops.db.loader` has been run.

These tests may read ground_truth.json: tests are part of evaluation.
The application and agents never read it.
"""

import json
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from payops import health
from payops.config import get_settings
from payops.db.connection import get_engine, get_readonly_engine

pytestmark = pytest.mark.integration

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "generated"


@pytest.fixture(scope="module")
def settings():
    s = get_settings()
    if health.check_database(s).status != "ok":
        pytest.skip("PostgreSQL is not reachable (run: docker compose up -d db)")
    with get_engine(s).connect() as conn:
        if conn.execute(text("SELECT to_regclass('payments.dataset_runs')")).scalar() is None:
            pytest.skip("No dataset loaded (run: python -m payops.db.loader)")
    return s


@pytest.fixture(scope="module")
def manifest():
    path = DATA_DIR / "manifest.json"
    if not path.exists():
        pytest.skip("data/generated/manifest.json not found")
    return json.loads(path.read_text(encoding="utf-8"))


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


# ---- Loaded data matches the files -----------------------------------------

def test_loaded_dataset_is_the_generated_one(settings, manifest):
    loaded = scalar(get_engine(settings), "SELECT dataset_id FROM payments.dataset_runs")
    assert loaded == manifest["dataset_id"], "Database holds a different dataset; re-run the loader"


@pytest.mark.parametrize(
    "file_name, table",
    [
        ("providers.csv", "payments.providers"),
        ("provider_incidents.csv", "payments.provider_incidents"),
        ("deployments.csv", "payments.deployments"),
        ("transactions.csv", "payments.transactions"),
    ],
)
def test_row_counts_match_manifest(settings, manifest, file_name, table):
    assert scalar(get_engine(settings), f"SELECT count(*) FROM {table}") == manifest["files"][file_name]["rows"]


# ---- SQL aggregation correctness --------------------------------------------

def test_daily_stats_totals_equal_transaction_totals(settings):
    eng = get_engine(settings)
    with eng.connect() as conn:
        tx = conn.execute(text(
            "SELECT count(*), count(*) FILTER (WHERE status='FAILED'), sum(amount), "
            "coalesce(sum(amount) FILTER (WHERE status='FAILED'), 0) FROM payments.transactions"
        )).one()
        ds = conn.execute(text(
            "SELECT sum(attempts), sum(failures), sum(total_value), sum(failed_value) FROM payments.daily_stats"
        )).one()
    assert tuple(tx) == tuple(ds)


def test_sql_failure_counts_match_pandas_on_the_csv(settings):
    csv = pd.read_csv(
        DATA_DIR / "transactions.csv",
        usecols=["provider_code", "status", "amount"],
        dtype={"provider_code": "string", "status": "string"},
    )
    expected = (
        csv[csv["status"] == "FAILED"].groupby("provider_code")
        .agg(failures=("status", "size"), failed_value=("amount", "sum"))
    )
    with get_engine(settings).connect() as conn:
        rows = conn.execute(text(
            "SELECT provider_code, count(*), sum(amount) FROM payments.transactions "
            "WHERE status = 'FAILED' GROUP BY provider_code"
        )).all()
    for provider, failures, failed_value in rows:
        assert failures == expected.loc[provider, "failures"]
        assert float(failed_value) == pytest.approx(expected.loc[provider, "failed_value"], abs=0.01)


# ---- The answer key stays out of the database -------------------------------

def test_ground_truth_is_not_in_the_database(settings):
    eng = get_engine(settings)
    params = scalar(eng, "SELECT parameters::text FROM payments.dataset_runs")
    assert "incident" not in params.lower()
    assert scalar(eng, "SELECT count(*) FROM information_schema.tables WHERE table_name ILIKE '%truth%'") == 0


# ---- The injected incident is discoverable with plain SQL ------------------

def test_sql_finds_the_injected_provider_and_reason(settings):
    truth = json.loads((DATA_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    primary = next(i for i in truth["injected_incidents"] if i["key"] == "primary")
    with get_readonly_engine(settings).connect() as conn:
        top = conn.execute(text("""
            WITH p AS (SELECT date_trunc('week', window_end) AS lw_end FROM payments.dataset_runs)
            SELECT provider_code
            FROM payments.transactions, p
            WHERE created_at >= lw_end - interval '14 days' AND created_at < lw_end
            GROUP BY provider_code
            ORDER BY avg((status='FAILED')::int) FILTER (WHERE created_at >= lw_end - interval '7 days')
                   - avg((status='FAILED')::int) FILTER (WHERE created_at <  lw_end - interval '7 days') DESC
            LIMIT 1
        """)).scalar()
        reason = conn.execute(text("""
            WITH p AS (SELECT date_trunc('week', window_end) AS lw_end FROM payments.dataset_runs)
            SELECT failure_reason FROM payments.transactions, p
            WHERE provider_code = :provider AND status = 'FAILED'
              AND created_at >= lw_end - interval '7 days' AND created_at < lw_end
            GROUP BY 1 ORDER BY count(*) DESC LIMIT 1
        """), {"provider": top}).scalar()
    assert top == primary["provider_code"]
    assert reason == primary["failure_reason"]


# ---- The read-only account really is read-only -------------------------------

def test_readonly_account_can_read(settings):
    assert scalar(get_readonly_engine(settings), "SELECT count(*) FROM payments.providers") > 0


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO payments.providers VALUES ('EVIL', 'Evil Corp')",
        "UPDATE payments.transactions SET amount = 1",
        "DELETE FROM payments.transactions",
        "CREATE TABLE payments.evil (i int)",
        "DROP TABLE payments.transactions",
    ],
)
def test_readonly_account_cannot_write_even_if_it_turns_off_read_only_mode(settings, statement):
    with get_readonly_engine(settings).connect() as conn:
        # Layer 1: sessions start read-only.
        with pytest.raises(DBAPIError):
            conn.execute(text(statement))
        conn.rollback()
        # Layer 2: even after switching read-only mode off, privileges refuse the write.
        conn.execute(text("SET default_transaction_read_only = off"))
        conn.commit()
        with pytest.raises(DBAPIError, match="permission denied|must be owner"):
            conn.execute(text(statement))
        conn.rollback()
    get_readonly_engine(settings).dispose()  # don't return a modified session to the pool


def test_readonly_account_has_a_query_time_limit(settings):
    assert scalar(get_readonly_engine(settings), "SHOW statement_timeout") == "30s"
