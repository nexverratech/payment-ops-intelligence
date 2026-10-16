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
| Agent inputs/outputs: findings, evidence, hypotheses, trace | `src/payops/agents/schemas.py` |
| Shared workflow state (append-only lists for parallel steps) | `src/payops/agents/state.py` |
| Tool permissions and tool-call recording | `src/payops/agents/tools.py` |
| Plan + final assembly (Operations Coordinator) | `src/payops/agents/coordinator.py` |
| Transaction Intelligence Agent | `src/payops/agents/transaction_agent.py` |
| Pattern Investigation Agent + hypothesis ranking | `src/payops/agents/pattern_agent.py` |
| Forecasting Agent | `src/payops/agents/forecast_agent.py` |
| Business Impact Agent | `src/payops/agents/impact_agent.py` |
| LangGraph wiring, parallel branch, error capture, step limit | `src/payops/agents/graph.py` |
| Text report without LLM + trace table | `src/payops/agents/template_report.py` |
| Command-line investigation | `src/payops/agents/run.py` |
| Local LLM client (timeout, budget, JSON schema, retry) | `src/payops/llm/client.py` |
| LLM prompts (question/data inside tags) | `src/payops/llm/prompts.py` |
| LLM plan validation | `src/payops/agents/coordinator.py` (`validate_proposal`) |
| Report writing by the LLM; choice of LLM vs template report | `src/payops/agents/coordinator.py` (`narrate`, `finalize`) |
| Verification and Policy Agent | `src/payops/agents/verification_agent.py` |
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

## Agent path (Phase 5a)

```
python -m payops.agents.run "question"
  → agents/graph.run_investigation(read-only engine, question)
      → plan         coordinator.plan        AgentTools("coordinator"): dataset_window
      → in parallel: transaction_agent.run   AgentTools("transaction"): summary, breakdown, failure_reasons, samples
                     pattern_agent.run       AgentTools("pattern"): time_series → detect_anomalies, events, hypotheses
                     forecast_agent.run      AgentTools("forecast"): time_series → forecast_daily
      → impact       impact_agent.run        AgentTools("impact"): summary → window_impact
      → assemble     coordinator.assemble    findings + hypotheses + uncertainties + failed/skipped steps
  → template_report.render + render_trace
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

---

## Phase 5a: agents (`src/payops/agents/`)

Full design, permissions and hypothesis rules: `docs/agent_design.md`.

| File | Purpose and main items | Inputs → outputs | Errors handled | Tests |
|---|---|---|---|---|
| `schemas.py` | `ToolCall`, `Evidence`, `Finding`, `Hypothesis`, `Plan`, `TraceEvent`, `AgentError`, one findings model per agent | typed objects between agents | Pydantic validation | all agent tests |
| `state.py` | `InvestigationState`: shared dictionary; `trace`/`tool_calls`/`errors` are append-only (reducers) | — | — | `test_agent_graph.py` |
| `tools.py` | `AgentTools(agent, engine)`, `TOOL_PERMISSIONS`, `ToolPermissionError` | agent requests → recorded calls to `analytics.queries` | Unknown agent or disallowed tool → `ToolPermissionError`, recorded as `denied`; failed calls recorded as `failed` | `test_agents.py` |
| `coordinator.py` | `plan()` (deterministic in 5a), `assemble()` (headline, findings, uncertainties, failed/skipped steps) | question → plan; full state → result | Too little history → `ValueError` with a clear message | `test_agents.py`, `test_agent_graph.py` |
| `transaction_agent.py` | `run()`: overall change + z-test, provider and method comparison, top contributor, reason changes, sample IDs | plan → `TransactionFindings` (TX-1…TX-4) | No payments → `ValueError` | `test_agents.py` |
| `pattern_agent.py` | `run()`, `rank_hypotheses()` | plan → anomalies, related events, ranked hypotheses (PT-n) | No anomaly → an honest "nothing detected" finding | `test_agents.py` |
| `forecast_agent.py` | `run()` | plan → volume and failure-rate forecast with backtest (FC-1, FC-2) | Too little history → "No forecast" finding | `test_agents.py` |
| `impact_agent.py` | `run()` | anomaly window (or period comparison) → labelled figures (IM-1…IM-3) | Falls back to period comparison if the pattern step failed | `test_agents.py` |
| `graph.py` | `build_graph()`, `run_investigation()`, `_node()` wrapper, `NODES`, `MAX_STEPS` | question → JSON-ready result | Per-step error capture → `partial`; generic messages for unexpected errors; skipped steps when inputs are missing | `test_agent_graph.py`, `integration/test_agents_live.py` |
| `template_report.py` | `render()`, `render_trace()` | result → text (labelled "no LLM") | — | `test_agent_graph.py` |
| `run.py` | command line | question → printed report and trace; exit code 2 if partial | — | manual |

## New tests in Phase 5a

| File | What it proves |
|---|---|
| `tests/test_agents.py` | Tool permissions enforced and recorded; plan logic; each agent's findings and evidence; hypothesis ranking rules, including a case where the deployment should lead; honest "no anomaly" and "no forecast" results; impact fallback |
| `tests/test_agent_graph.py` | Full LangGraph run: all six steps, impact after the parallel three, every cited tool call exists, a crashing agent gives a partial result without leaking internals, a failed plan skips everything downstream |
| `tests/integration/test_agents_live.py` | On real data: workflow completes, the leading hypothesis is the injected provider, the correlated deployment is not blamed, every finding is traceable, no tool call was denied |

---

## Phase 5b: local LLM and verification

| File | Purpose and main items | Inputs → outputs | Errors handled | Tests |
|---|---|---|---|---|
| `llm/client.py` | `OllamaClient.generate(purpose, system, user, schema)`, `LLMClient` protocol, `LLMCall`, `LLMUnavailable` / `LLMOutputError` / `LLMBudgetExceeded` | prompt + Pydantic schema → validated object | Connection/timeout/HTTP errors → `LLMUnavailable`; invalid JSON → one retry, then `LLMOutputError`; budget → `LLMBudgetExceeded`; every call recorded | `test_llm_client.py` |
| `llm/prompts.py` | `PLANNER_SYSTEM`, `planner_user`, `NARRATOR_SYSTEM`, `narrator_user` | question / findings → prompt text with data inside tags | — | `test_planner.py` |
| `agents/coordinator.py` | `plan` (LLM proposal → `validate_proposal` → `Plan`), `assemble`, `narrate`, `finalize`, `render_llm_report` | see `docs/agent_design.md` | Any LLM problem or rejected proposal → default plan or template report, with the reason recorded | `test_planner.py`, `test_agent_graph_llm.py` |
| `agents/verification_agent.py` | `run`, `check_sentence`, `extract_numbers` | state + narrative → `VerificationReport` + verified narrative | Missing steps → checks marked `skipped` | `test_verification.py`, `test_agent_graph_llm.py` |
| `agents/schemas.py` (additions) | `PlanProposal`, `Sentence`, `NarrativeSection`, `NarrativeReport`, `VerificationCheck`, `VerificationReport` | what the LLM may return, and what verification reports | — | — |
| `api/routes_investigations.py` | Runs the workflow with a fresh `OllamaClient` per request (`use_llm` can switch it off) | question → status, mode, reason, full result, report text | Never fails the request because of the LLM or one agent | `test_api_endpoints.py` |
| `dashboard/app.py` (Investigate tab) | Report with citation tags; expanders for verification, findings and evidence, hypotheses, agent trace | — | API errors shown on screen | manual |

## New tests in Phase 5b

| File | What it proves |
|---|---|
| `tests/fake_llm.py` | Scripted LLM for repeatable tests (not a test itself) |
| `tests/test_llm_client.py` | Request is local, structured, no thinking, temperature 0; fenced JSON accepted; retry then error; unavailable and missing-model handling; budget enforced; non-local URL refused |
| `tests/test_verification.py` | Number extraction ignores IDs/dates/times; invented numbers, numbers from uncited findings, missing or unknown citations, causal and proof claims are caught; hedged wording passes |
| `tests/test_planner.py` | Proposal validation (dates, weeks, codes, injection-style codes); accepted plan is labelled; rejected or failed LLM → default plan; question passed inside tags |
| `tests/test_agent_graph_llm.py` | Good report → `llm` mode with 100% support; bad report → template with reasons; LLM down → labelled template; injected question can't widen access |
| `tests/integration/test_llm_live.py` | The real `qwen3:4b` on real data: output is verified or replaced by the template; plan valid if accepted (skipped without Ollama) |

---

## Phase 6: history, checkpoints and resilience

| File | Purpose and main items | Inputs → outputs | Errors handled | Tests |
|---|---|---|---|---|
| `db/history_schema.sql` | Schema `ops`: `investigations`, `investigation_steps` | — | Idempotent (`IF NOT EXISTS`), never drops | `integration/test_history_live.py` |
| `db/setup_history.py` | `python -m payops.db.setup_history`: creates the schema and the history login with least privilege | `.env` → schema + login | Missing password or shared login name → clear stop; DB down → message | `integration/test_history_live.py` (permissions) |
| `db/connection.py` | `get_history_engine` | settings → engine for the history login | Raises if history not configured | `test_config.py` |
| `history/codec.py` | `encode_update` / `decode_update` (state update ↔ JSON, validated against `InvestigationState`), `encode_json` (NaN → null) | — | Unknown fields dropped; wrong shapes rejected | `test_history_codec.py` |
| `history/store.py` | `InvestigationStore` interface; `PostgresStore`; `MemoryStore` (tests); `InvestigationRecord`, `StepRecord`; `NotFound`, `NotResumable` | records and step rows | Atomic claim for resume; stale-run detection | `test_checkpoints.py`, `test_api_history.py`, `integration/test_history_live.py` |
| `agents/graph.py` | `RunContext`, `_node` (checkpoint, replay, time budget), `run_investigation(..., store, timeout_seconds)`, `resume_investigation`, `latest_reusable`, `reusable_steps`, `UPSTREAM` | question / id → result with `persisted`, `run`, `reused_steps` | History outage → `persisted: false`; Ctrl+C or crash → `interrupted`; data reloaded → resume refused | `test_checkpoints.py` |
| `agents/tools.py` | `is_transient`, retry once, `MAX_CALLS_PER_STEP`, `ToolLimitError`, `ToolCall.attempts` | — | Timeout and SQL errors not retried | `test_tools_resilience.py` |
| `agents/coordinator.py` | `finalize` now counts steps after `assemble` (a failed or timed-out report makes the run partial and resumable); `LLM_FAILURE_NOTE` | — | — | `test_checkpoints.py` |
| `agents/run.py` | `--list`, `--show ID`, `--resume ID`; Ctrl+C prints the resume command | — | Non-UUID id → message; history off → message | manual |
| `api/routes_investigations.py` | `GET /investigations`, `GET /investigations/{id}`, `POST /investigations/{id}/resume`; `get_store`; concurrency slots | — | 404, 409, 422 (bad id), 429 (busy), 503 (history not configured) | `test_api_history.py` |
| `health.py` | `check_history`: `ok` / `not_configured` / `not_set_up` / `unreachable` | — | Never raises | `test_health_api.py` |
| `dashboard/app.py` | `show_report` (shared view), **History** tab with Open and Resume | — | API errors shown on screen | `test_dashboard_app.py` |
| `docs/runbook.md` | Recovery procedures | — | — | — |

## New tests in Phase 6

| File | What it proves |
|---|---|
| `tests/test_checkpoints.py` | Every step saved; resume reuses finished steps and really skips them; resumed result equals a fresh run; evidence still traceable; stopped process → interrupted → resumable; heartbeat detection; complete/unknown/concurrent/reloaded-data resumes refused; reuse rules; failed LLM report retried; time limit → labelled partial report, then resume; failed report step → partial; history outage doesn't break a run |
| `tests/test_history_codec.py` | Every step's output survives save/load unchanged; real types restored; unknown fields dropped; tampered data rejected; NaN → null; an LLM report and its verification survive |
| `tests/test_tools_resilience.py` | Dropped connection retried once and recorded; two failures reported; statement timeout and SQL errors not retried; call limit enforced |
| `tests/test_api_history.py` | Save → list → open; filters and paging validated; resume over HTTP; 409, 404, 422, 503, 429 |
| `tests/integration/test_history_live.py` | Real database: history login can't read payments or delete history; read-only login can't see history; saved with every step; partial run resumes to the same answer (skipped without history set up) |

---

## Phase 7: security and evaluation

| File | Purpose and main items | Errors handled | Tests |
|---|---|---|---|
| `security/passwords.py` | `hash_password`, `verify_password` (scrypt, constant-time compare), `needs_rehash`, `check_policy`, `burn_time` | Malformed or absurd stored hashes never verify | `test_passwords.py` |
| `security/rbac.py` | `Permission`, `ROLE_PERMISSIONS`, `can`; the only place access rules live | Unknown role → no permissions | `test_api_auth.py` (matrix) |
| `security/audit.py` | `AuditEvent` (validated, small, no floats), `chain_hash`, `verify_chain` | Reports the first changed or orphaned row | `test_audit_chain.py` |
| `security/store.py` | `AuthStore`; `PostgresAuthStore` (advisory lock serialises the audit chain); `MemoryAuthStore` | Duplicate / unknown user errors | `test_auth_service.py`, `integration/test_auth_live.py` |
| `security/service.py` | `login` (lockout, generic errors, rehash), `resolve` (idle/absolute expiry, disabled), `logout`, `create_user`, `change_role_or_status` (last-admin guard), `set_password` | `AuthError` (public message + audit reason), `AccountError` | `test_auth_service.py` |
| `security/manage.py` | CLI: create-user, list-users, set-role, disable/enable, reset-password, unlock, audit, verify-audit; audited as `cli:<user>` | Refusals printed, exit code 1/2 | `test_security_manage.py` |
| `db/security_schema.sql` | `ops.users`, `ops.sessions`, `ops.audit_log` (+ append-only triggers), `ops.investigations.created_by` | CHECK constraints on names, roles, hashes, actions | `integration/test_auth_live.py` |
| `db/setup_app.py` | Runs both schema files; creates `payops_app` with exact grants (`setup_history` now forwards here) | — | `integration/` |
| `api/auth.py` | `current_user`, `require(*permissions)`, `audit`, `deny`, `get_auth_store` | 401 / 403 (+ audit) / 503 when not configured | `test_api_auth.py` |
| `api/routes_auth.py` | `/auth/login`, `/auth/logout`, `/auth/me`, `/auth/password`, `/users...`, `/audit`, `/audit/verify` | 401, 403, 404, 422 | `test_api_auth.py` |
| `api/routes_investigations.py` | Permissions plus ownership (404 for others' investigations); start/finish/view/resume audited | — | `test_api_auth.py`, `test_api_history.py` |
| `api/main.py` | `BodySizeLimit` (413), security headers | — | `test_api_auth.py` |
| `api/schemas.py` | Questions with control or text-direction characters refused | 422 | `test_api_auth.py` |
| `dashboard/app.py` | Sign-in form, per-session client and token-keyed cache, sign-out, tabs by role, Audit log tab | 401 → back to sign-in | `test_dashboard_app.py` |
| `evaluation/llm_eval.py` | 10 cases with expected plans; plan accuracy, LLM report rate, support rate, safety, latency; JSON + Markdown output | A crashing case is recorded, not fatal | `test_llm_eval.py` |
| `tests/auth_helpers.py` | `sign_in_as(app, role)` for tests that are not about sign-in | — | — |

## New tests in Phase 7

| File | What it proves |
|---|---|
| `test_passwords.py` | OWASP scrypt parameters in production; right/wrong password; random salts; garbage hashes rejected; upgrades flagged; policy |
| `test_auth_service.py` | Sessions carry role permissions; generic failures; lockout and its expiry; disabled accounts; logout, idle and absolute expiry; role change and password change end sessions; last-admin guard; invalid and duplicate accounts |
| `test_audit_chain.py` | Untouched log verifies; changed and removed rows detected; links; event validation |
| `test_api_auth.py` | No unprotected route; sign-in/out; 401s; generic audited failures without the password; fail-closed 503; role matrix; refusals audited; ownership (404) for read and resume; admin account management without hashes; reset ends sessions; chain verifies over the API; 413; hidden characters refused; security headers |
| `test_security_manage.py` | CLI bootstrap and audit source; weak passwords refused; role/disable/unlock; tampering reported |
| `test_llm_eval.py` | Cases well formed; plan and safety scoring; a crashing case recorded |
| `test_dashboard_app.py` (extended) | Sign-in form only without a session; sign-in stores the token in the session; tabs by role; expired session returns to sign-in |
| `integration/test_auth_live.py` | Real sign-in to real data; logout immediate; only token hashes stored; audit append-only for both logins; app login can't read payments; the real chain verifies |

---

## Phase 8: packaging and CI

| File | Purpose | Tests |
|---|---|---|
| `Dockerfile` | One image for API, dashboard and tools; Python 3.11 slim; packages from `requirements.lock`; non-root; telemetry off | `test_packaging.py`; CI job `docker` |
| `.dockerignore` | Keeps `.env`, data, `.git`, tests and docs out of the build | `test_packaging.py` |
| `docker-compose.yml` | `db` (as before); profile `app`: `setup` (once), `api`, `dashboard`; profile `tools` | `test_packaging.py`; CI job `docker` |
| `.github/workflows/ci.yml` | Jobs `tests`, `docker`, `dependency-audit` | `test_packaging.py` |
| `scripts/check_test_report.py` | CI gate: fails on failures, unexpected skips or too few tests | `test_ci_report.py` |
| `netpolicy.py`, `config.py` | `LOCAL_SERVICE_HOSTS` (container names as local, single words only); owner password optional; `owner_password()` for owner-level scripts | `test_config.py`, `test_netpolicy.py` |
| `health.py` | Database check uses the read-only login | `test_health_api.py` |
| `security/manage.py` | `--password-stdin` for scripts and CI | `test_security_manage.py` |
| `db/setup_app.py` | Works before data is loaded (the read-only login may not exist yet) | CI job `docker` |
