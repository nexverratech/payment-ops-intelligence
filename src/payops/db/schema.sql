-- Payment Operations Intelligence: analytical schema.
--
-- Run by payops.db.loader as the database owner, inside ONE transaction:
-- if any statement fails, nothing changes.
-- Rebuilding is safe because all data is regenerated from the seed.

SET TIME ZONE 'UTC';

DROP SCHEMA IF EXISTS payments CASCADE;
CREATE SCHEMA payments;
COMMENT ON SCHEMA payments IS 'Synthetic payment data. Read-only for the application and agents.';

-- ---------------------------------------------------------------------------
-- Lookup table: the payment providers (gateways) we route payments to.
-- ---------------------------------------------------------------------------
CREATE TABLE payments.providers (
    provider_code  TEXT PRIMARY KEY,                -- e.g. 'KESTREL'
    display_name   TEXT NOT NULL UNIQUE             -- e.g. 'Kestrel Gateway'
);

-- ---------------------------------------------------------------------------
-- Notices that providers published on their status pages.
-- reported_* times are what the provider SAID, which can differ from when
-- the problem really started.
-- ---------------------------------------------------------------------------
CREATE TABLE payments.provider_incidents (
    incident_id        TEXT PRIMARY KEY,                                   -- 'INC-0003'
    provider_code      TEXT NOT NULL REFERENCES payments.providers (provider_code),
    reported_start_at  TIMESTAMPTZ NOT NULL,
    reported_end_at    TIMESTAMPTZ,                                        -- NULL = still open
    severity           TEXT NOT NULL CHECK (severity IN ('info', 'minor', 'major', 'critical')),
    title              TEXT NOT NULL,
    description        TEXT,
    CHECK (reported_end_at IS NULL OR reported_end_at > reported_start_at)
);

-- ---------------------------------------------------------------------------
-- Software releases to our own production services.
-- ---------------------------------------------------------------------------
CREATE TABLE payments.deployments (
    deployment_id   TEXT PRIMARY KEY,                -- 'DEP-0026'
    service         TEXT NOT NULL,
    version         TEXT NOT NULL,
    deployed_at     TIMESTAMPTZ NOT NULL,
    environment     TEXT NOT NULL CHECK (environment IN ('production', 'staging')),
    team            TEXT NOT NULL,
    change_summary  TEXT,
    UNIQUE (service, version)
);

-- ---------------------------------------------------------------------------
-- One row per payment attempt. The core fact table.
-- ---------------------------------------------------------------------------
CREATE TABLE payments.transactions (
    transaction_id     TEXT PRIMARY KEY,                                   -- 'TXN-00000001'
    created_at         TIMESTAMPTZ NOT NULL,
    customer_id        TEXT NOT NULL,                                      -- synthetic, e.g. 'CUST-002718'
    provider_code      TEXT NOT NULL REFERENCES payments.providers (provider_code),
    payment_method     TEXT NOT NULL CHECK (payment_method IN ('CARD', 'BANK_TRANSFER', 'WALLET', 'NET_BANKING')),
    amount             NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    currency           CHAR(3) NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('SUCCEEDED', 'FAILED')),
    failure_reason     TEXT CHECK (failure_reason IN (
                           'INSUFFICIENT_FUNDS', 'DO_NOT_HONOR', 'AUTHENTICATION_FAILED',
                           'FRAUD_SUSPECTED', 'INVALID_DETAILS', 'TIMEOUT', 'PROVIDER_ERROR')),
    latency_ms         INTEGER NOT NULL CHECK (latency_ms >= 0),
    provider_event_id  TEXT REFERENCES payments.provider_incidents (incident_id),
    -- A failed payment must have a reason; a successful one must not.
    CONSTRAINT failure_reason_matches_status CHECK ((status = 'FAILED') = (failure_reason IS NOT NULL))
);

-- Indexes: the questions we ask most are "what happened in this time range,
-- per provider" and "show me the failures".
CREATE INDEX transactions_created_at_idx      ON payments.transactions (created_at);
CREATE INDEX transactions_provider_time_idx   ON payments.transactions (provider_code, created_at);
CREATE INDEX transactions_failed_time_idx     ON payments.transactions (created_at) WHERE status = 'FAILED';

-- ---------------------------------------------------------------------------
-- Daily totals per provider and method, built from transactions by the loader.
-- The dashboard reads this instead of scanning ~500,000 rows each time.
-- ---------------------------------------------------------------------------
CREATE TABLE payments.daily_stats (
    stat_date        DATE NOT NULL,                  -- UTC calendar day
    provider_code    TEXT NOT NULL REFERENCES payments.providers (provider_code),
    payment_method   TEXT NOT NULL,
    attempts         INTEGER NOT NULL CHECK (attempts > 0),
    failures         INTEGER NOT NULL CHECK (failures BETWEEN 0 AND attempts),
    total_value      NUMERIC(16, 2) NOT NULL,
    failed_value     NUMERIC(16, 2) NOT NULL,
    avg_latency_ms   NUMERIC(10, 1) NOT NULL,
    PRIMARY KEY (stat_date, provider_code, payment_method)
);

-- ---------------------------------------------------------------------------
-- Which dataset is loaded, so every result can be traced to a seed.
-- Deliberately does NOT contain the ground truth (answer key).
-- ---------------------------------------------------------------------------
CREATE TABLE payments.dataset_runs (
    dataset_id        TEXT PRIMARY KEY,
    seed              INTEGER NOT NULL,
    window_start      TIMESTAMPTZ NOT NULL,
    window_end        TIMESTAMPTZ NOT NULL,
    parameters        JSONB NOT NULL,
    files             JSONB NOT NULL,
    library_versions  JSONB NOT NULL,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE payments.transactions IS 'One row per synthetic payment attempt.';
COMMENT ON COLUMN payments.transactions.provider_event_id IS 'Provider notice referenced in the provider response, if any. Only present after the notice was published.';
COMMENT ON TABLE payments.daily_stats IS 'Derived: daily totals per provider and payment method. Rebuilt on every load.';
