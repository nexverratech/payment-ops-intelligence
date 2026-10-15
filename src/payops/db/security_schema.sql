-- Payment Operations Intelligence: accounts, sessions and the audit log (Phase 7).
--
-- Run by `python -m payops.db.setup_app` as the database owner, after history_schema.sql.
-- Safe to run again: creates what is missing, never deletes data.

SET TIME ZONE 'UTC';

-- Local accounts. Passwords are stored only as scrypt hashes (see security/passwords.py).
CREATE TABLE IF NOT EXISTS ops.users (
    username             TEXT PRIMARY KEY CHECK (username ~ '^[a-z][a-z0-9_.-]{2,31}$'),
    role                 TEXT NOT NULL CHECK (role IN ('viewer', 'analyst', 'lead', 'admin')),
    password_hash        TEXT NOT NULL CHECK (password_hash LIKE 'scrypt$%'),
    active               BOOLEAN NOT NULL DEFAULT true,
    failed_logins        INTEGER NOT NULL DEFAULT 0 CHECK (failed_logins >= 0),
    locked_until         TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    password_changed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Server-side sessions. Only the SHA-256 of the token is stored, never the token itself.
CREATE TABLE IF NOT EXISTS ops.sessions (
    token_hash    TEXT PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    username      TEXT NOT NULL REFERENCES ops.users (username) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS sessions_username_idx ON ops.sessions (username);

-- Append-only, hash-chained audit log (see security/audit.py).
CREATE TABLE IF NOT EXISTS ops.audit_log (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at         TIMESTAMPTZ NOT NULL,
    actor      TEXT NOT NULL,
    role       TEXT,
    action     TEXT NOT NULL CHECK (action ~ '^[a-z_]+\.[a-z_]+$'),
    target     TEXT,
    outcome    TEXT NOT NULL CHECK (outcome IN ('success', 'denied', 'failure')),
    source     TEXT NOT NULL CHECK (source IN ('api', 'cli')),
    details    JSONB NOT NULL DEFAULT '{}',
    prev_hash  CHAR(64) NOT NULL,
    hash       CHAR(64) NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS audit_log_actor_idx ON ops.audit_log (actor);
CREATE INDEX IF NOT EXISTS audit_log_action_idx ON ops.audit_log (action);

-- Nobody, not even the owner, can change or remove audit rows by accident.
-- (A superuser could still drop the trigger first; the hash chain then shows the change.)
CREATE OR REPLACE FUNCTION ops.audit_log_is_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ops.audit_log is append-only (% refused)', TG_OP;
END
$$;
DROP TRIGGER IF EXISTS audit_log_no_change ON ops.audit_log;
CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON ops.audit_log
    FOR EACH ROW EXECUTE FUNCTION ops.audit_log_is_append_only();
DROP TRIGGER IF EXISTS audit_log_no_truncate ON ops.audit_log;
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON ops.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION ops.audit_log_is_append_only();

-- Who started each investigation (NULL for runs saved before Phase 7).
ALTER TABLE ops.investigations ADD COLUMN IF NOT EXISTS created_by TEXT;
CREATE INDEX IF NOT EXISTS investigations_created_by_idx ON ops.investigations (created_by);
