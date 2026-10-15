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
