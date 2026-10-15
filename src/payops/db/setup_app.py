"""Create the application schema ("ops") and its database login.

Usage (once after the database is running; safe to repeat, e.g. after an upgrade):

    python -m payops.db.setup_app

As the database owner, in one transaction:
1. history_schema.sql   investigations and their step checkpoints (Phase 6)
2. security_schema.sql  accounts, sessions, append-only audit log (Phase 7)
3. Creates or updates the login APP_DB_USER (password APP_DB_PASSWORD) with exactly:

       ops.investigations, ops.investigation_steps   SELECT, INSERT, UPDATE
       ops.users, ops.sessions                       SELECT, INSERT, UPDATE
       ops.audit_log                                 SELECT, INSERT   (no UPDATE/DELETE)

   and nothing in schema "payments". No DELETE anywhere.

The three logins:

    owner (POSTGRES_USER)   everything; only setup scripts, the loader and health checks use it
    payops_readonly         SELECT on payments.*; every session read-only
    payops_app              the grants above; cannot see payments.*
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg
from psycopg import sql

from payops.config import Settings, get_settings

HERE = Path(__file__).parent
SCHEMA_FILES = (HERE / "history_schema.sql", HERE / "security_schema.sql")
GRANTS = {
    "ops.investigations": "SELECT, INSERT, UPDATE",
    "ops.investigation_steps": "SELECT, INSERT, UPDATE",
    "ops.users": "SELECT, INSERT, UPDATE",
    "ops.sessions": "SELECT, INSERT, UPDATE",
    "ops.audit_log": "SELECT, INSERT",
}


def setup(settings: Settings) -> None:
    if not settings.history_enabled:
        raise SystemExit("APP_DB_PASSWORD is not set in .env; add it first (see .env.example).")
    if settings.app_db_user in {settings.postgres_user, settings.readonly_db_user}:
        raise SystemExit("APP_DB_USER must be a separate login from POSTGRES_USER and READONLY_DB_USER.")
    # Keyword-style connection, like the loader: the password is never put into a URL string.
    with psycopg.connect(host=settings.postgres_host, port=settings.postgres_port, dbname=settings.postgres_db,
                         user=settings.postgres_user, password=settings.postgres_password.get_secret_value(),
                         connect_timeout=int(settings.health_check_timeout_seconds) or 3) as conn, conn.cursor() as cur:
        for f in SCHEMA_FILES:
            cur.execute(f.read_text(encoding="utf-8"))
        role = sql.Identifier(settings.app_db_user)
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (settings.app_db_user,))
        if cur.fetchone() is None:
            cur.execute(sql.SQL("CREATE ROLE {} LOGIN").format(role))
        cur.execute(sql.SQL("ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}").format(
            role, sql.Literal(settings.app_db_password.get_secret_value())))  # type: ignore[union-attr]
        cur.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '10s'").format(role))
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(settings.postgres_db), role))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA ops TO {}").format(role))
        for table, privileges in GRANTS.items():
            cur.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.SQL(table), role))
            cur.execute(sql.SQL("GRANT {} ON {} TO {}").format(sql.SQL(privileges), sql.SQL(table), role))
        # The read-only login (analytics, agents) gets nothing in ops.
        cur.execute(sql.SQL("REVOKE ALL ON SCHEMA ops FROM {}").format(sql.Identifier(settings.readonly_db_user)))


def main() -> int:
    settings = get_settings()
    try:
        setup(settings)
    except psycopg.OperationalError as exc:
        print(f"SETUP FAILED: cannot connect to PostgreSQL ({type(exc).__name__}). Is it running? docker compose ps")
        return 1
    print("Schema 'ops' is ready: " + ", ".join(GRANTS) + ".")
    print(f"Login '{settings.app_db_user}' has exactly these rights; it cannot read payment data or delete anything.")
    print("Next, if there is no admin yet:  python -m payops.security.manage create-user <name> --role admin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
