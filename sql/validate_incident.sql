-- Phase 2 validation: does the loaded data show the injected incident?
--
-- These queries do NOT read the answer key. They work out "last week" from the
-- loaded dataset and FIND the provider with the biggest change, the way an
-- analyst would. Compare what they find with data/generated/ground_truth.json.
--
-- Windows:  Get-Content sql\validate_incident.sql | docker exec -i payops-db psql -U payops_admin -d payops
-- Linux:    docker exec -i payops-db psql -U payops_admin -d payops < sql/validate_incident.sql

SET TIME ZONE 'UTC';
\pset footer off

\echo
\echo '== 1. What is loaded =='
SELECT dataset_id, seed, window_start, window_end,
       (SELECT count(*) FROM payments.transactions) AS transactions
FROM payments.dataset_runs;

-- "Last week" = the last complete Monday-Sunday week before the data ends.
CREATE TEMP VIEW period AS
SELECT date_trunc('week', window_end) - interval '7 days'  AS last_week_start,
       date_trunc('week', window_end)                      AS last_week_end,
       date_trunc('week', window_end) - interval '14 days' AS prev_week_start
FROM payments.dataset_runs;

\echo
\echo '== 2. Weekly failure rate (last 6 weeks) =='
SELECT date_trunc('week', created_at)::date                                   AS week_starting,
       count(*)                                                               AS attempts,
       count(*) FILTER (WHERE status = 'FAILED')                              AS failures,
       round(100.0 * count(*) FILTER (WHERE status = 'FAILED') / count(*), 2) AS failure_rate_pct
FROM payments.transactions, period
WHERE created_at >= last_week_end - interval '42 days' AND created_at < last_week_end
GROUP BY 1 ORDER BY 1;

\echo
\echo '== 3. Last week vs previous week, by provider (biggest change first) =='
WITH per_provider AS (
    SELECT provider_code,
           count(*) FILTER (WHERE created_at >= last_week_start)                            AS lw_attempts,
           count(*) FILTER (WHERE created_at >= last_week_start AND status = 'FAILED')      AS lw_failures,
           count(*) FILTER (WHERE created_at <  last_week_start)                            AS pw_attempts,
           count(*) FILTER (WHERE created_at <  last_week_start AND status = 'FAILED')      AS pw_failures
    FROM payments.transactions, period
    WHERE created_at >= prev_week_start AND created_at < last_week_end
    GROUP BY provider_code
)
SELECT provider_code,
       round(100.0 * pw_failures / pw_attempts, 2)                                    AS prev_week_rate_pct,
       round(100.0 * lw_failures / lw_attempts, 2)                                    AS last_week_rate_pct,
       round(100.0 * lw_failures / lw_attempts - 100.0 * pw_failures / pw_attempts, 2) AS change_pct_points,
       lw_failures - pw_failures                                                      AS extra_failures
FROM per_provider
ORDER BY change_pct_points DESC;

\echo
\echo '== 4. Failure reasons last week for the provider with the biggest increase =='
WITH top_provider AS (
    SELECT provider_code
    FROM payments.transactions, period
    WHERE created_at >= prev_week_start AND created_at < last_week_end
    GROUP BY provider_code
    ORDER BY avg((status = 'FAILED')::int) FILTER (WHERE created_at >= last_week_start)
           - avg((status = 'FAILED')::int) FILTER (WHERE created_at <  last_week_start) DESC
    LIMIT 1
)
SELECT t.provider_code, t.failure_reason, count(*) AS failures
FROM payments.transactions t, period, top_provider tp
WHERE t.provider_code = tp.provider_code AND t.status = 'FAILED'
  AND t.created_at >= last_week_start AND t.created_at < last_week_end
GROUP BY 1, 2 ORDER BY failures DESC;

\echo
\echo '== 5. That provider in 6-hour blocks across last week (when did it start and stop?) =='
WITH top_provider AS (
    SELECT provider_code
    FROM payments.transactions, period
    WHERE created_at >= prev_week_start AND created_at < last_week_end
    GROUP BY provider_code
    ORDER BY avg((status = 'FAILED')::int) FILTER (WHERE created_at >= last_week_start)
           - avg((status = 'FAILED')::int) FILTER (WHERE created_at <  last_week_start) DESC
    LIMIT 1
)
SELECT date_bin('6 hours', t.created_at, last_week_start)                              AS block_start,
       count(*)                                                                         AS attempts,
       round(100.0 * count(*) FILTER (WHERE status = 'FAILED') / count(*), 1)           AS failure_rate_pct,
       count(*) FILTER (WHERE failure_reason = 'TIMEOUT')                               AS timeouts,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)::numeric, 0)      AS median_latency_ms,
       count(provider_event_id)                                                         AS rows_citing_provider_notice
FROM payments.transactions t, period, top_provider tp
WHERE t.provider_code = tp.provider_code
  AND t.created_at >= last_week_start AND t.created_at < last_week_end
GROUP BY 1 ORDER BY 1;

\echo
\echo '== 6. Timeline: provider notices and deployments in the last 10 days =='
SELECT events.* FROM (
    SELECT reported_start_at AS at, 'PROVIDER NOTICE' AS kind, incident_id AS id,
           provider_code || ': ' || title AS what
    FROM payments.provider_incidents
    UNION ALL
    SELECT deployed_at, 'DEPLOYMENT', deployment_id, service || ' ' || version || ' (' || change_summary || ')'
    FROM payments.deployments
) events, period
WHERE at >= last_week_end - interval '10 days' AND at < last_week_end
ORDER BY at;

\echo
\echo '== 7. Overall failure rate 24 h before vs 24 h after each deployment last week =='
\echo '   (a big jump AFTER a deployment is a correlation to investigate, not proof of cause)'
SELECT d.deployment_id, d.service, d.version, d.deployed_at,
       round(100.0 * avg((t.status = 'FAILED')::int) FILTER (WHERE t.created_at <  d.deployed_at), 2) AS rate_24h_before_pct,
       round(100.0 * avg((t.status = 'FAILED')::int) FILTER (WHERE t.created_at >= d.deployed_at), 2) AS rate_24h_after_pct
FROM payments.deployments d
JOIN payments.transactions t
  ON t.created_at >= d.deployed_at - interval '24 hours' AND t.created_at < d.deployed_at + interval '24 hours'
CROSS JOIN period
WHERE d.deployed_at >= last_week_start - interval '3 days' AND d.deployed_at < last_week_end
GROUP BY 1, 2, 3, 4 ORDER BY 4;
