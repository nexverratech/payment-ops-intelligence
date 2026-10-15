# Offline Operation Checklist

Goal: after the one-time setup, the whole project works with the network disconnected.
Items are added as each phase is built. ✅ = has an automated check, 🖐 = manual check.

## How offline behaviour is enforced in code

1. **Configuration refuses non-local addresses.** `src/payops/config.py` rejects any `POSTGRES_HOST` or `OLLAMA_BASE_URL` that is not `localhost` / `127.x.x.x` / `::1`, and rejects Ollama `-cloud` model names.
2. **Tests block the internet.** `tests/conftest.py` installs `payops.netpolicy.install_network_guard()` for the whole test run. Any connection or DNS lookup for a non-local host raises `NetworkPolicyError` and fails the test.
3. **No CDN-loaded pages.** FastAPI's `/docs` page is disabled because it fetches JavaScript from the internet.

## Phase 1 checklist

| # | Check | How | Type |
|---|---|---|---|
| 1 | Non-local Ollama or DB address is rejected | `pytest tests/test_config.py` | ✅ |
| 2 | Outbound connections are blocked during tests | `pytest tests/test_netpolicy.py` | ✅ |
| 3 | `/docs` (CDN page) is disabled | `pytest tests/test_health_api.py` | ✅ |
| 4 | Ollama cloud is off | `server.log` contains `Ollama cloud disabled: true` | 🖐 |
| 5 | Database and API listen only on 127.0.0.1 | `netstat -ano | findstr ":5432 :8000"` shows `127.0.0.1`, not `0.0.0.0` | 🖐 |
| 6 | Full Phase 1 workflow offline | Disconnect network → `pytest` → start API → `/health` | 🖐 |

## Known network behaviour outside our code

| Component | Behaviour | Effect offline |
|---|---|---|
| Ollama (Windows) | Downloads app updates automatically | Skipped; no impact |
| Docker Desktop | Update checks; image pulls | Already-pulled images work offline |

## Phase 2 checklist

| # | Check | How | Type |
|---|---|---|---|
| 7 | Data generation needs no network | Generator uses only numpy/pandas on local files; tests run under the network guard | ✅ |
| 8 | Loading needs no network | Loader streams local CSVs to the local container | 🖐 (offline run) |
| 9 | Analytical access is read-only | `pytest tests/integration/test_loaded_data.py` | ✅ |
| 10 | Postgres image already downloaded | `docker image ls postgres` shows `16` before going offline | 🖐 |
