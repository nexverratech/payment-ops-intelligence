"""Loads a generated dataset into PostgreSQL.

Run:  python -m payops.db.loader --data-dir data/generated

Steps (all inside ONE database transaction, so a failure leaves the database unchanged):
1. Check every CSV against the fingerprint (SHA-256) in manifest.json.
2. Rebuild the `payments` schema from schema.sql.
3. Bulk-load the CSVs with PostgreSQL COPY (much faster than row-by-row INSERTs).
4. Build the derived daily_stats table.
5. Record the dataset in payments.dataset_runs.
6. Create/update the read-only login and grant it SELECT only.
7. Check that row counts in the database match the manifest.

ground_truth.json is never opened here: the answer key must not reach the database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from payops.config import Settings, get_settings

log = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# Load order matters: a table must be loaded before the tables that reference it.
TABLES: tuple[tuple[str, str], ...] = (
    ("providers.csv", "payments.providers"),
    ("provider_incidents.csv", "payments.provider_incidents"),
    ("deployments.csv", "payments.deployments"),
    ("transactions.csv", "payments.transactions"),
)

BUILD_DAILY_STATS = """
INSERT INTO payments.daily_stats
    (stat_date, provider_code, payment_method, attempts, failures, total_value, failed_value, avg_latency_ms)
SELECT
    (created_at AT TIME ZONE 'UTC')::date,
    provider_code,
    payment_method,
    count(*),
    count(*) FILTER (WHERE status = 'FAILED'),
    sum(amount),
    coalesce(sum(amount) FILTER (WHERE status = 'FAILED'), 0),
    round(avg(latency_ms), 1)
FROM payments.transactions
GROUP BY 1, 2, 3
"""


class LoadError(RuntimeError):
    """The dataset is missing, corrupted or does not match its manifest."""


def read_manifest(data_dir: Path) -> dict:
    path = data_dir / "manifest.json"
    if not path.exists():
        raise LoadError(f"{path} not found. Generate data first: python -m payops.data.generator")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_files(data_dir: Path, manifest: dict) -> None:
    """Refuse to load files that were edited or truncated after generation."""
    for file_name, _ in TABLES:
        expected = manifest["files"].get(file_name)
        path = data_dir / file_name
        if expected is None or not path.exists():
            raise LoadError(f"{file_name} is missing from {data_dir} or from the manifest")
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != expected["sha256"]:
            raise LoadError(f"{file_name} does not match its manifest fingerprint (file changed or corrupted)")


def load(data_dir: Path, settings: Settings) -> dict[str, int]:
    """Load the dataset. Returns {table: row count}."""
    manifest = read_manifest(data_dir)
    verify_files(data_dir, manifest)

    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.owner_password(),
        connect_timeout=int(settings.health_check_timeout_seconds) or 3,
    ) as conn:
        # psycopg starts a transaction automatically; leaving the `with` block
        # commits it, and an exception rolls everything back.
        with conn.cursor() as cur:
            cur.execute(SCHEMA_FILE.read_text(encoding="utf-8"))

            for file_name, table in TABLES:
                started = time.perf_counter()
                with (data_dir / file_name).open("rb") as f:
                    header = f.readline().decode("utf-8").strip().split(",")
                    copy_sql = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv)").format(
                        sql.SQL(table), sql.SQL(", ").join(map(sql.Identifier, header))
                    )
                    with cur.copy(copy_sql) as copy:
                        for chunk in iter(lambda: f.read(1 << 20), b""):
                            copy.write(chunk)
                log.info("loaded table", extra={"fields": {"table": table, "seconds": round(time.perf_counter() - started, 2)}})

            cur.execute(BUILD_DAILY_STATS)
            cur.execute(
                """INSERT INTO payments.dataset_runs
                   (dataset_id, seed, window_start, window_end, parameters, files, library_versions)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    manifest["dataset_id"], manifest["seed"], manifest["window_start"], manifest["window_end"],
                    Jsonb(manifest["parameters"]), Jsonb(manifest["files"]), Jsonb(manifest["library_versions"]),
                ),
            )
            _setup_readonly_role(cur, settings)
            cur.execute("ANALYZE")  # refresh planner statistics so queries use the indexes well

            counts = _row_counts(cur)
            for file_name, table in TABLES:
                if counts[table] != manifest["files"][file_name]["rows"]:
                    raise LoadError(
                        f"{table}: {counts[table]} rows in database, manifest says {manifest['files'][file_name]['rows']}"
                    )
    return counts


def _setup_readonly_role(cur: psycopg.Cursor, settings: Settings) -> None:
    role = sql.Identifier(settings.readonly_db_user)
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (settings.readonly_db_user,))
    if cur.fetchone() is None:
        cur.execute(sql.SQL("CREATE ROLE {} LOGIN").format(role))
    # sql.Literal safely quotes the password; it is never pasted into SQL as raw text.
    cur.execute(
        sql.SQL("ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
            role, sql.Literal(settings.readonly_db_password.get_secret_value())
        )
    )
    # Defence in depth: even if a GRANT were wrong, every session of this role is read-only,
    # and no single query may run longer than 30 seconds.
    cur.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role))
    cur.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '30s'").format(role))
    cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(settings.postgres_db), role))
    cur.execute(sql.SQL("GRANT USAGE ON SCHEMA payments TO {}").format(role))
    cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA payments TO {}").format(role))


def _row_counts(cur: psycopg.Cursor) -> dict[str, int]:
    counts = {}
    for table in [t for _, t in TABLES] + ["payments.daily_stats", "payments.dataset_runs"]:
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.SQL(table)))
        counts[table] = cur.fetchone()[0]
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load a generated dataset into PostgreSQL.")
    parser.add_argument("--data-dir", type=Path, default=Path("data/generated"))
    args = parser.parse_args(argv)

    from payops.logging_setup import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level)
    started = time.perf_counter()
    try:
        counts = load(args.data_dir, settings)
    except LoadError as exc:
        print(f"LOAD FAILED: {exc}")
        return 1
    except psycopg.OperationalError as exc:
        print(f"LOAD FAILED: cannot connect to PostgreSQL ({type(exc).__name__}). Is it running? docker compose ps")
        return 1

    print(f"Loaded dataset {read_manifest(args.data_dir)['dataset_id']} in {time.perf_counter() - started:.1f} s")
    for table, n in counts.items():
        print(f"  {table:<32} {n:>10,} rows")
    print(f"Read-only login '{settings.readonly_db_user}' can SELECT from schema 'payments' and nothing else.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
