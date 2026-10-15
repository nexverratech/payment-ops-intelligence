-- Payment Operations Intelligence: investigation history and checkpoints.
--
-- Run by `python -m payops.db.setup_history` as the database owner.
-- Safe to run again: it creates what is missing and never deletes anything.
-- Kept in its own schema ("ops"), so reloading the payment data, which drops and
-- rebuilds schema "payments", never touches saved investigations.

SET TIME ZONE 'UTC';

CREATE SCHEMA IF NOT EXISTS ops;
COMMENT ON SCHEMA ops IS 'Investigation history and step checkpoints. Written only by the history login.';
REVOKE ALL ON SCHEMA ops FROM PUBLIC;

-- One row per investigation: what was asked, how it ended, the full result.
CREATE TABLE IF NOT EXISTS ops.investigations (
    investigation_id  UUID PRIMARY KEY,
    question          TEXT NOT NULL CHECK (char_length(question) BETWEEN 1 AND 2000),
    as_of             TIMESTAMPTZ,                       -- NULL = end of the loaded data
    use_llm           BOOLEAN NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('running', 'complete', 'partial', 'interrupted')),
    mode              TEXT CHECK (mode IN ('llm', 'template')),
    mode_reason       TEXT,
    headline          TEXT,
    dataset_id        TEXT,                              -- the data the investigation was run on
    runs              INTEGER NOT NULL DEFAULT 1 CHECK (runs >= 1),   -- 1 + number of resumes
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(), -- heartbeat: refreshed after every step
    finished_at       TIMESTAMPTZ,
    duration_ms       INTEGER,
    result            JSONB                               -- the full structured result (as returned by the API)
);
CREATE INDEX IF NOT EXISTS investigations_created_idx ON ops.investigations (created_at DESC);
CREATE INDEX IF NOT EXISTS investigations_status_idx ON ops.investigations (status);

-- One row per step per run: the step's output (a checkpoint) or the reason it did not finish.
-- A resumed run reuses a step's latest 'ok' + reusable row instead of running the step again.
CREATE TABLE IF NOT EXISTS ops.investigation_steps (
    investigation_id  UUID NOT NULL REFERENCES ops.investigations (investigation_id) ON DELETE CASCADE,
    step              TEXT NOT NULL CHECK (step IN ('plan', 'transaction', 'pattern', 'forecast', 'impact',
                                                    'assemble', 'narrate', 'verify', 'finalize')),
    run               INTEGER NOT NULL CHECK (run >= 1),
    status            TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'skipped')),
    reusable          BOOLEAN NOT NULL,                   -- may a resumed run use this output?
    payload           JSONB NOT NULL,                     -- the step's state update, validated JSON
    duration_ms       INTEGER NOT NULL,
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (investigation_id, step, run)
);
