# File Guide

What every file does, where its logic lives, and how to test it. Updated each phase.

## Feature → file map

| Feature | File(s) |
|---|---|
| Settings and `.env` loading | `src/payops/config.py` |
| Local-only network rule | `src/payops/netpolicy.py` |
| JSON logs | `src/payops/logging_setup.py` |
| Database connection pool | `src/payops/db/connection.py` |
| Database and LLM health checks | `src/payops/health.py` |
| `GET /health` endpoint | `src/payops/api/main.py` |
| PostgreSQL container | `docker-compose.yml` |
| Scenario settings (providers, rates, injected incidents) | `src/payops/data/scenario.py` |
| Synthetic data generator, manifest, ground truth | `src/payops/data/generator.py` |
| Database tables, keys, constraints, indexes | `src/payops/db/schema.sql` |
| Loading data, read-only account | `src/payops/db/loader.py` |
| Read-only database engine | `src/payops/db/connection.py` (`get_readonly_engine`) |
| SQL checks that find the injected incident | `sql/validate_incident.sql` |
| Filters, figure labels (measured / estimate / hypothetical) | `src/payops/analytics/models.py` |
| Failure rate, period comparison, contribution share, z-test | `src/payops/analytics/metrics.py` |
| Incident detection baseline | `src/payops/analytics/detection.py` |
| Deployments / provider notices near an anomaly | `src/payops/analytics/events.py` |
| Business impact figures | `src/payops/analytics/impact.py` |
| Volume and failure-rate forecast | `src/payops/analytics/forecast.py` |
| Parameterized analytics SQL (read-only account) | `src/payops/analytics/queries.py` |
| Weekly deterministic report (and future LLM fallback) | `src/payops/analytics/report.py` |
| Detection precision / recall across seeds | `src/payops/analytics/detection_eval.py` |
| API: connection + validated filter dependencies | `src/payops/api/deps.py` |
| API: request/response shapes | `src/payops/api/schemas.py` |
| API: KPIs, trends, breakdowns, reasons, events, anomalies | `src/payops/api/routes_analytics.py` |
| API: investigation endpoint | `src/payops/api/routes_investigations.py` |
| API: error handling (503 / 500 without internals) | `src/payops/api/main.py` |
| Dashboard page, filters, tabs | `src/payops/dashboard/app.py` |
| Dashboard → API calls | `src/payops/dashboard/api_client.py` |
| Charts and provider colours | `src/payops/dashboard/charts.py` |
| Streamlit offline settings | `.streamlit/config.toml` |
| Test setup and internet block | `tests/conftest.py` |

## Request path (Phase 1)

```
PowerShell: Invoke-RestMethod http://127.0.0.1:8000/health
  → uvicorn receives the request
  → api/main.py get_health()
      → config.get_settings()            (cached Settings from .env)
      → health.check_database(settings)
            → db/connection.get_engine() → PostgreSQL: SELECT 1
      → health.check_llm(settings)
            → httpx2 GET 127.0.0.1:11434/api/tags → is OLLAMA_MODEL installed?
      → decide ok / degraded / down → HealthResponse (JSON)
      → JSON log line written to stdout
```

## Data path (Phase 2)

```
python -m payops.data.generator
  → scenario.GenerationConfig (seed 42, 90 days, incidents)
  → generator.generate()      numpy + pandas build 4 DataFrames + ground truth
  → generator.write_dataset() CSVs + manifest.json + ground_truth.json
python -m payops.db.loader
  → verify SHA-256 of each CSV against manifest.json
  → ONE transaction: schema.sql → COPY CSVs → build daily_stats → dataset_runs
                     → read-only role + grants → ANALYZE → row counts == manifest
```

## Analytics path (Phase 3)

```
python -m payops.analytics.report
  → db/connection.get_readonly_engine()        (read-only account, UTC sessions)
  → report.build_report(conn)
      → queries.dataset_window / summary / breakdown / failure_reasons   (SQL, bound parameters)
      → metrics.compare_periods          → which provider explains the rise
      → queries.time_series (hourly)     → detection.detect_anomalies
      → queries.events                   → events.related_events + events.change_around
      → impact.window_impact             → measured / estimate / hypothetical figures
      → queries.time_series (daily)      → forecast.forecast_daily (+ backtest)
  → report.render_text(...) or JSON
```

## Dashboard path (Phase 4)

```
Browser (127.0.0.1:8501)
  → dashboard/app.py               filters in the sidebar → Filters(start, end, providers, ...)
  → dashboard/api_client.py        GET /api/v1/kpis?start=...&provider=KESTREL   (cached 60 s)
  → api/main.py → routes_analytics.kpis()
      → deps.get_readonly_conn     read-only database account
      → deps.analysis_filter       AnalysisFilter validation → 422 with a readable message if invalid
      → analytics.queries.summary  parameterized SQL (current and previous period)
  → JSON → dashboard/charts.py → Plotly figure → st.plotly_chart
"Investigate" tab → POST /api/v1/investigations → analytics.report.build_report (deterministic, labelled)
```

Later phases extend this path: Streamlit → FastAPI → LangGraph → agents → database → verification → report.

---

## `src/payops/netpolicy.py`

1. **Purpose:** turns "no data leaves this computer" into code.
2. **Problem solved:** a mistyped URL or a library default could silently call the internet. This makes such calls fail loudly.
3. **Logic:** `is_loopback_host` accepts only `localhost`, 127.0.0.0/8 and `::1` (not `0.0.0.0`). `require_local_url` also checks the scheme. `install_network_guard` temporarily replaces `socket.connect`, `connect_ex` and `getaddrinfo` with versions that raise `NetworkPolicyError` for non-local hosts.
4. **Main items:** `is_loopback_host`, `require_local_url`, `install_network_guard`, `NetworkPolicyError`.
5. **Called by:** `config.py` (validators), `tests/conftest.py` (guard). Later: the offline check script.
6. **Imports:** standard library only (`ipaddress`, `socket`, `urllib.parse`).
7. **Inputs/outputs:** host or URL string → bool / validated string; the guard returns an `uninstall()` function.
8. **Errors:** raises `ValueError` for bad URLs and `NetworkPolicyError` for blocked connections.
9. **Test:** `pytest tests/test_netpolicy.py`
10. **Example:** `require_local_url("https://ollama.com")` → `ValueError: URL host 'ollama.com' is not local...`

## `src/payops/config.py`

1. **Purpose:** single source of all settings.
2. **Problem solved:** settings scattered through code, secrets in source files, and late, confusing failures from bad values.
3. **Logic:** `Settings` (pydantic-settings) reads environment variables, then `.env`. Validators reject non-local hosts and `-cloud` models. The password is a `SecretStr`, so it shows as `**********` when printed. `database_url()` builds the URL with `URL.create()`, which escapes special characters and masks the password in logs.
4. **Main items:** `Settings`, `Settings.database_url()`, `get_settings()` (cached).
5. **Called by:** `api/main.py`, `health.py`, `db/connection.py`, tests.
6. **Imports:** pydantic, pydantic-settings, SQLAlchemy `URL`, `netpolicy`.
7. **Inputs/outputs:** environment / `.env` → validated `Settings` object.
8. **Errors:** invalid or missing values raise `pydantic.ValidationError` at startup, naming the field.
9. **Test:** `pytest tests/test_config.py`
10. **Example:** with `POSTGRES_PORT=70000` the app refuses to start: `postgres_port: Input should be less than or equal to 65535`.

## `src/payops/logging_setup.py`

1. **Purpose:** one JSON object per log line.
2. **Problem solved:** plain-text logs are hard to search by field (status, investigation ID, agent name).
3. **Logic:** `JsonFormatter` writes timestamp, level, logger, message and any `extra={"fields": {...}}`. Exceptions are reduced to type and message.
4. **Main items:** `JsonFormatter`, `configure_logging(level)`.
5. **Called by:** `api/main.py` at startup.
6. **Imports:** standard library only.
7. **Inputs/outputs:** log records → JSON lines on stdout.
8. **Errors:** non-JSON values are converted with `str()` instead of crashing.
9. **Test:** visible when running the API; dedicated tests arrive with audit logging in Phase 7.
10. **Example:** `{"ts": "...", "level": "INFO", "logger": "payops.api", "msg": "health check", "status": "ok", "database": "ok", "llm": "ok"}`

## `src/payops/db/connection.py`

1. **Purpose:** creates the shared database connection pool.
2. **Problem solved:** opening a new connection per query is slow; a stale connection after a container restart causes errors.
3. **Logic:** `get_engine(settings)` returns a cached SQLAlchemy `Engine` with a pool of 5 (+5 overflow), `pool_pre_ping` and a short connect timeout. Cached by URL because `Settings` is not hashable.
4. **Main items:** `get_engine`.
5. **Called by:** `health.py`; from Phase 2, all database code.
6. **Imports:** SQLAlchemy, `config`.
7. **Inputs/outputs:** `Settings` → `Engine`.
8. **Errors:** creating the engine does not connect; connection errors appear on first use and are handled by the caller.
9. **Test:** `pytest tests/integration` (needs the container).
10. **Example:** `with get_engine(s).connect() as c: c.execute(text("SELECT 1"))`.

## `src/payops/health.py`

1. **Purpose:** checks the database and the local LLM.
2. **Problem solved:** "is everything running?" needs a single, reliable answer that never crashes.
3. **Logic:** `check_database` runs `SELECT 1`. `check_llm` calls Ollama's `/api/tags` and checks the configured model is installed. Both catch every exception and return a `ComponentHealth` with status `ok`, `unreachable` or `model_missing`, a latency, and a safe hint. Only the exception *type* is returned, so connection details can't leak.
4. **Main items:** `ComponentHealth`, `check_database`, `check_llm`.
5. **Called by:** `api/main.py`, integration tests.
6. **Imports:** httpx2, SQLAlchemy, pydantic, `config`, `db.connection`.
7. **Inputs/outputs:** `Settings` → `ComponentHealth`.
8. **Errors:** handled inside each function; never raises.
9. **Test:** `pytest tests/test_health_api.py tests/integration`
10. **Example:** Ollama stopped → `ComponentHealth(status="unreachable", detail="ConnectError. Start Ollama, or continue in fallback mode.")`

## `src/payops/api/main.py`

1. **Purpose:** the FastAPI application. Phase 1 has only `GET /health`.
2. **Problem solved:** gives the dashboard, scripts and Docker a standard way to check readiness.
3. **Logic:** `create_app()` configures logging, disables `/docs` (CDN), and defines `get_health`. Rules: database down → HTTP 503 `down`; LLM unavailable → `degraded` with `llm_mode: fallback`; otherwise `ok`.
4. **Main items:** `create_app`, `app`, `HealthResponse`.
5. **Called by:** uvicorn; `TestClient` in tests.
6. **Imports:** FastAPI, pydantic, `config`, `health`, `logging_setup`.
7. **Inputs/outputs:** HTTP GET → JSON `HealthResponse`.
8. **Errors:** component failures are converted to statuses; invalid settings stop startup with a `ValidationError`.
9. **Test:** `pytest tests/test_health_api.py`
10. **Example:** `Invoke-RestMethod http://127.0.0.1:8000/health`

## `docker-compose.yml`

Runs PostgreSQL 16 as container `payops-db`. It reads the database name, user and password from `.env`, binds the port to **127.0.0.1 only**, stores data in the named volume `payops_pgdata`, and reports healthy via `pg_isready`. Phase 8 adds the API and dashboard services.

## `tests/conftest.py`

Puts test settings into environment variables before the app is imported. Database values come from your real environment or `.env` (so integration tests can connect); other values are fixed test values. Installs the network guard for the whole session.

## Tests

| File | What it proves |
|---|---|
| `tests/test_netpolicy.py` | Local hosts allowed, remote hosts and DNS blocked |
| `tests/test_config.py` | Bad or non-local settings are rejected; password never printed |
| `tests/test_health_api.py` | ok / degraded / down logic, no password in responses, `/docs` disabled |
| `tests/integration/test_database_live.py` | Real PostgreSQL reachable, version ≥ 16 (skipped if container is off) |

## Configuration files

| File | Purpose |
|---|---|
| `.env.example` | Template for `.env`, placeholders only |
| `.gitignore` | Keeps `.env`, the venv and generated data out of Git |
| `requirements.in` | Direct dependencies with minimum versions |
| `requirements.lock` | Exact versions, generated by you after tests pass (`pip freeze`) |
| `pyproject.toml` | Project metadata and pytest settings (`pythonpath = src`) |

---

## `src/payops/data/scenario.py`

1. **Purpose:** every number that shapes the synthetic world (providers, traffic, failure rates, injected incidents).
2. **Problem solved:** keeps the "story" in one readable place instead of magic numbers scattered through the generator.
3. **Logic:** frozen dataclasses only, no randomness. Incidents are positioned relative to the **end** of the window, so they stay in "last week" whatever the number of days.
4. **Main items:** `ProviderProfile`, `InjectedIncident`, `GenerationConfig` (`to_dict()`).
5. **Called by:** `generator.py`, tests.
6. **Imports:** standard library only.
7. **Inputs/outputs:** none / configuration objects.
8. **Errors:** invalid combinations are caught by the generator CLI (e.g. `--days` < 35).
9. **Test:** indirectly through `tests/test_generator.py`.
10. **Example:** `GenerationConfig(seed=7, days=40)` gives a smaller, different, reproducible world.

## `src/payops/data/generator.py`

1. **Purpose:** creates the synthetic dataset and its answer key.
2. **Problem solved:** we need realistic data with a **known** incident, so we can later measure whether the system finds it.
3. **Logic:** hourly payment counts (daily and weekly rhythm, growth) → provider, method, customer, amount → normal failures with day-to-day wobble → injected incidents (extra failures with a set reason and a ramp up/down) → provider-notice references only after the notice time → deployments, two of them placed on purpose. `write_dataset` uses fixed formats and `\n` line endings, so files are byte-identical across operating systems for the same library versions.
4. **Main items:** `generate(config) -> GeneratedData`, `write_dataset(data, config, out_dir) -> manifest`, `main()`.
5. **Called by:** you (`python -m payops.data.generator`), tests.
6. **Imports:** numpy, pandas, `scenario`.
7. **Inputs/outputs:** a config → 4 CSVs, `manifest.json`, `ground_truth.json`.
8. **Errors:** argparse rejects bad arguments; everything else is deterministic code.
9. **Test:** `pytest tests/test_generator.py` (14 tests: reproducibility, data rules, incident visibility, deployments).
10. **Example:** `python -m payops.data.generator --seed 1 --out data/seed1` creates a second, different dataset.

## `src/payops/db/schema.sql`

Creates schema `payments` with `providers`, `provider_incidents`, `deployments`, `transactions`, `daily_stats` and `dataset_runs`. It defines primary keys, foreign keys, CHECK rules (e.g. a failure reason is present exactly when a payment failed) and indexes for time-range and per-provider queries. It starts with `DROP SCHEMA ... CASCADE`, so it always rebuilds from scratch. See `docs/data_dictionary.md`.

## `src/payops/db/loader.py`

1. **Purpose:** puts a generated dataset into PostgreSQL safely.
2. **Problem solved:** loading must be fast, all-or-nothing, and must never load corrupted files or the answer key.
3. **Logic:** check SHA-256 fingerprints → in **one transaction**: run `schema.sql`, `COPY` each CSV (bulk load), build `daily_stats`, record `dataset_runs`, create/update the read-only account, `ANALYZE`, compare row counts with the manifest. Any failure rolls everything back.
4. **Main items:** `load()`, `verify_files()`, `read_manifest()`, `_setup_readonly_role()`, `LoadError`, `main()`.
5. **Called by:** you (`python -m payops.db.loader`).
6. **Imports:** psycopg (`sql` for safe identifiers and quoting), `config`.
7. **Inputs/outputs:** a data folder → loaded tables; prints row counts.
8. **Errors:** `LoadError` (missing/changed files, count mismatch) and connection errors print a one-line reason and exit with code 1. SQL errors roll back the transaction.
9. **Test:** `pytest tests/integration/test_loaded_data.py` after loading.
10. **Example:** editing one character in `transactions.csv` → `LOAD FAILED: transactions.csv does not match its manifest fingerprint`.

**The read-only account, two layers of protection:**
1. Every session of `payops_readonly` starts read-only (`default_transaction_read_only = on`), and queries stop after 30 s (`statement_timeout`).
2. Even if a session switches read-only mode off, the account only has `SELECT` privileges, so writes fail with "permission denied".

Tests check both layers. The agents (Phase 5) will only ever get this account.

## `sql/validate_incident.sql`

Seven plain-SQL queries an analyst would run: what is loaded, weekly failure rate, last week vs previous week per provider, failure reasons for the biggest mover, 6-hour blocks, timeline of notices and deployments, and failure rate 24 h before/after each deployment. They **discover** the provider from the data rather than naming it. Run with `Get-Content sql\validate_incident.sql | docker exec -i payops-db psql -U payops_admin -d payops`.

## New tests in Phase 2

| File | What it proves |
|---|---|
| `tests/test_generator.py` | Same seed → identical files; data rules hold; the incident raises failures only at the affected provider with the right reason; notice references appear only after publication; the answer key stays out of the manifest |
| `tests/integration/test_loaded_data.py` | The loaded dataset is the generated one; row counts match; SQL totals equal pandas totals on the CSV; `daily_stats` equals `transactions`; plain SQL finds the injected provider and reason; the read-only account cannot write; the 30 s query limit is set |

## Other files added in Phase 2

| File | Purpose |
|---|---|
| `.gitattributes` | Stores text files with LF line endings in Git (removes the CRLF warnings); PowerShell scripts keep CRLF |
| `docs/data_dictionary.md` | Every table and column, and the injected scenario |

---

## Phase 3: analytics (`src/payops/analytics/`)

All files follow one rule: **SQL fetches small aggregated tables; plain pandas/numpy functions do every calculation.** That's why each calculation can be unit-tested with hand-made numbers. See `docs/analytics_methods.md` for every formula in plain English.

| File | Purpose and main functions | Inputs → outputs | Errors handled | Tests |
|---|---|---|---|---|
| `models.py` | `AnalysisFilter` (validated time range and codes), `Figure` (value + unit + kind + method), `AnomalyWindow`, `RelatedEvent`, `ForecastResult` | user/agent input → validated objects | Rejects naive datetimes, end ≤ start, spans > 400 days, codes that aren't UPPER_CASE | `test_analytics_metrics.py` |
| `metrics.py` | `failure_rate`, `two_proportion_test`, `compare_periods`, `last_complete_week` | two tables [key, attempts, failures] → comparison table | Impossible counts raise `ValueError`; zero attempts give NaN, not 0 | `test_analytics_metrics.py` |
| `detection.py` | `score_hours`, `detect_anomalies`, `_merge_runs` | hourly [segment, hour, attempts, failures] → list of `AnomalyWindow` | Missing hours filled with 0; hours without a baseline are never flagged | `test_analytics_detection.py` |
| `events.py` | `related_events`, `change_around` | window + deployments + notices → events with time offsets; hourly + a moment → before/after table with significance | Open-ended notices treated as running to the window end | `test_analytics_detection.py` |
| `impact.py` | `ImpactAssumptions`, `window_impact` | counts, values, expected failures, duration → list of labelled `Figure`s | Rejects zero duration and impossible counts; negative excess never produces negative costs | `test_analytics_impact_forecast.py` |
| `forecast.py` | `forecast_daily` → (volume, failure rate) | daily [date, attempts, failures] → `ForecastResult` with backtest vs naive baseline | Under 28 days → `insufficient_data`; 28–41 days → forecast without backtest, stated | `test_analytics_impact_forecast.py` |
| `queries.py` | `dataset_window`, `summary`, `breakdown`, `failure_reasons`, `time_series`, `events`, `sample_failed_transactions` | read-only connection + filter → DataFrames | Groupings and time units come from fixed dictionaries (`KeyError` otherwise); all values are bound parameters | `test_analytics_queries.py`, `integration/test_analytics_sql.py` |
| `report.py` | `build_report` (dict, JSON-ready), `render_text`, `main` | connection (+ as-of) → full weekly review | Refuses a week with no previous week to compare against | `integration/test_analytics_sql.py` |
| `detection_eval.py` | `evaluate_seed`, `main` | seeds → precision and recall vs ground truth | Uses ground truth, so it's evaluation code, never used by the app | run manually; results in `docs/evaluation.md` |

**Example:**

```powershell
python -m payops.analytics.report                 # text report for the last complete week
python -m payops.analytics.report --json          # same, as JSON (what the API will return)
python -m payops.analytics.detection_eval         # precision / recall over 8 seeds
```

## New tests in Phase 3

| File | What it proves |
|---|---|
| `tests/test_analytics_metrics.py` | Rates, z-test and comparison match hand calculations; week boundaries; filter validation rejects bad and injection-style input |
| `tests/test_analytics_detection.py` | A 4-hour spike becomes one window with hand-computed excess and z; single hours, early data and steady series raise nothing; gap merging; event offsets; "not evidence of cause" on every relationship |
| `tests/test_analytics_impact_forecast.py` | Impact arithmetic and kind labels; forecast refuses short history, recovers a perfect trend, beats the naive baseline on trending data |
| `tests/test_analytics_queries.py` | Values never appear in SQL text; only whitelisted groupings and time units |
| `tests/integration/test_analytics_sql.py` | SQL equals pandas on the CSV (totals, per provider, per method, with filters); detection finds the primary incident with zero false alarms; the report reaches the ground-truth conclusion |

---

## Phase 4: API (`src/payops/api/`) and dashboard (`src/payops/dashboard/`)

| File | Purpose and main items | Inputs → outputs | Errors handled | Tests |
|---|---|---|---|---|
| `api/deps.py` | `get_readonly_conn` (one read-only connection per request), `analysis_filter` (query parameters → validated `AnalysisFilter`; defaults to the last complete week) | HTTP query → `AnalysisFilter` | Invalid filters → HTTP 422 with short messages | `test_api_endpoints.py` |
| `api/schemas.py` | `KpiResponse`, `TableResponse`, `InvestigationRequest` (5–2,000 characters, whitespace collapsed, timezone required), `InvestigationResponse` | JSON ↔ typed objects | Pydantic → 422 | `test_api_endpoints.py` |
| `api/routes_analytics.py` | `meta`, `kpis`, `timeseries`, `breakdown`, `failure_reasons`, `events`, `anomalies`. Each comparison uses the previous period **of the same length**. | filter → JSON tables built by Phase 3 functions | Only `provider`/`method` groupings and `hour`/`day` units are accepted | `test_api_endpoints.py`, `integration/test_api_live.py` |
| `api/routes_investigations.py` | `POST /api/v1/investigations`: validates the question, runs the deterministic report, labels it `mode: deterministic` | question → id, timing, report, text | Not enough data → 422. The question text is not logged. | same |
| `api/main.py` | App setup, routers, `/health`, error handlers | — | Database down → 503; query error → 500 "Database query failed."; anything else → 500 "Internal error." Never returns SQL, passwords or tracebacks. | same |
| `dashboard/api_client.py` | `ApiClient` (one method per endpoint), `Filters` (→ repeated query parameters), `ApiError` | Python calls → HTTP to the local API | Refuses non-local URLs; turns "connection refused", timeouts and 4xx/5xx into readable `ApiError`s | `test_dashboard_client.py` |
| `dashboard/charts.py` | `failure_rate_trend`, `volume_trend`, `provider_excess_bar`, `reasons_bar`; fixed `PROVIDER_COLORS` | rows → Plotly figures | Unknown provider → neutral grey, never a new colour | `test_dashboard_charts.py` |
| `dashboard/app.py` | Streamlit page: sidebar filters and status, KPI tiles, 5 tabs | user clicks → API calls → charts and tables | Any `ApiError` is shown on screen and stops that page run | `test_dashboard_app.py` (runs the real script headless) |

**Why the dashboard calls the API instead of the database:** the API is the single place that validates input and, from Phase 7, checks who may see what. A second path straight to the data would bypass both.

**Colour choices:** each provider keeps one colour everywhere (NovaPay blue, Kestrel orange, BlueRiver aqua, Orbit yellow), so filtering never repaints a provider. The four colours were checked for colour-blind separation. Aqua and yellow have low contrast on white, so every chart has a legend or labels, hover details and a table view.

## New tests in Phase 4

| File | What it proves |
|---|---|
| `tests/fakes.py` | Canned query results shared by the API and dashboard tests (not a test itself) |
| `tests/test_api_endpoints.py` | Default period and equal-length comparison; validation (incl. injection-style input) → 422; breakdown/reason/anomaly logic; investigation labelling and input limits; 503/500 without leaking internals |
| `tests/test_dashboard_client.py` | Repeated query parameters; POST body; readable errors; non-local API address refused |
| `tests/test_dashboard_charts.py` | One line per provider in its fixed colour; colours stable under filtering; event markers; bar sorting and labels |
| `tests/test_dashboard_app.py` | The real Streamlit script renders KPIs and tabs, and shows a clear error when the API is down |
| `tests/test_offline_config.py` | Streamlit telemetry off and bound to 127.0.0.1; dashboard → API address must be local; method/reason lists match the schema |
| `tests/integration/test_api_live.py` | Against the loaded data: KPIs consistent, KESTREL ranked first, anomaly + correlated deployment found, investigation end to end |
