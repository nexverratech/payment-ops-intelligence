"""Create the investigation-history schema and its database login.

Usage (once, after the database is running; safe to repeat):

    python -m payops.db.setup_history

What it does, as the database owner, in one transaction:
1. Creates schema "ops" and its two tables if they are missing (never drops anything).
2. Creates or updates the login APP_DB_USER with the password APP_DB_PASSWORD.
3. Grants that login SELECT, INSERT and UPDATE on the ops tables, and nothing else:
   no access to payment data, no DELETE (history is kept), no schema changes.

The three logins and what each may do:

    owner (POSTGRES_USER)     everything; used only by setup scripts and health checks
    payops_readonly           SELECT on payments.*; every session read-only
    payops_app (history)      SELECT/INSERT/UPDATE on ops.*; cannot see payments.*
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg
from psycopg import sql

from payops.config import Settings, get_settings

SCHEMA_FILE = Path(__file__).with_name("history_schema.sql")
TABLES = ("ops.investigations", "ops.investigation_steps")


def setup(settings: Settings) -> None:
    if not settings.history_enabled:
        raise SystemExit("APP_DB_PASSWORD is not set in .env; add it first (see .env.example).")
    if settings.app_db_user in {settings.postgres_user, settings.readonly_db_user}:
        raise SystemExit("APP_DB_USER must be a separate login from POSTGRES_USER and READONLY_DB_USER.")
    # Same keyword-style connection as the loader: the password is never put into a URL string.
    with psycopg.connect(host=settings.postgres_host, port=settings.postgres_port, dbname=settings.postgres_db,
                         user=settings.postgres_user, password=settings.postgres_password.get_secret_value(),
                         connect_timeout=int(settings.health_check_timeout_seconds) or 3) as conn, conn.cursor() as cur:
        cur.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
        role = sql.Identifier(settings.app_db_user)
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (settings.app_db_user,))
        if cur.fetchone() is None:
            cur.execute(sql.SQL("CREATE ROLE {} LOGIN").format(role))
        cur.execute(sql.SQL("ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}").format(
            role, sql.Literal(settings.app_db_password.get_secret_value())))  # type: ignore[union-attr]
        cur.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '10s'").format(role))
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(settings.postgres_db), role))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA ops TO {}").format(role))
        for table in TABLES:
            cur.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.SQL(table), role))
            cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE ON {} TO {}").format(sql.SQL(table), role))
        # The readonly login (analytics, agents) gets nothing in ops.
        cur.execute(sql.SQL("REVOKE ALL ON SCHEMA ops FROM {}").format(sql.Identifier(settings.readonly_db_user)))


def main() -> int:
    settings = get_settings()
    try:
        setup(settings)
    except psycopg.OperationalError as exc:
        print(f"SETUP FAILED: cannot connect to PostgreSQL ({type(exc).__name__}). Is it running? docker compose ps")
        return 1
    print(f"History schema 'ops' is ready ({', '.join(TABLES)}).")
    print(f"Login '{settings.app_db_user}' can SELECT/INSERT/UPDATE investigation history and nothing else.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
