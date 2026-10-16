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

## Phase 4 checklist

| # | Check | How | Type |
|---|---|---|---|
| 11 | Streamlit sends no usage statistics; listens on 127.0.0.1 only | `pytest tests/test_offline_config.py` | ✅ |
| 12 | Dashboard reaches the API only on a local address | `pytest tests/test_dashboard_client.py` (non-local URL refused) | ✅ |
| 13 | API's CDN-based `/docs` page is off | `pytest tests/test_health_api.py` | ✅ |
| 14 | Dashboard loads nothing from the internet | Disconnect the network, open the dashboard, press F12 → **Network** tab, reload: every request goes to `127.0.0.1` | 🖐 |

## Phase 5a checklist

| # | Check | How | Type |
|---|---|---|---|
| 15 | Hosted tracing (LangSmith) is off | `pytest tests/test_offline_config.py` | ✅ |
| 16 | The whole agent workflow makes no outbound connection | `pytest tests/test_agent_graph.py` (runs under the network guard) | ✅ |

## Phase 5b checklist

| # | Check | How | Type |
|---|---|---|---|
| 17 | The LLM client only talks to 127.0.0.1 | `pytest tests/test_llm_client.py` (non-local address refused) | ✅ |
| 18 | An investigation still works when Ollama is off | `pytest tests/test_agent_graph_llm.py` (labelled template report) | ✅ |
| 19 | Full investigation with the real model while offline | Disconnect the network → `python -m payops.agents.run "..."` → mode `llm` | 🖐 |

## Phase 6 checklist

| # | Check | How | Type |
|---|---|---|---|
| 20 | History is stored only in the local database | `pytest tests/integration/test_history_live.py`; the history login is created by `setup_history` against the local `POSTGRES_HOST` (validated local) | ✅ |
| 21 | Resume works offline | Disconnect the network → `python -m payops.agents.run "..."` → press Ctrl+C during the report → `--resume <id>` | 🖐 |
| 22 | No new dependency, so no new network behaviour | `docs/dependency_inventory.md` (Phase 6 section) | — |

## Phase 7 checklist

| # | Check | How | Type |
|---|---|---|---|
| 23 | Sign-in uses only local accounts, no external identity provider | `pytest tests/test_api_auth.py tests/integration/test_auth_live.py` (run under the network guard) | ✅ |
| 24 | Password hashing and tokens use only the Python standard library | `docs/dependency_inventory.md` (Phase 7: no new packages) | — |
| 25 | Model evaluation runs offline | Disconnect the network → `python -m payops.evaluation.llm_eval --cases 1 2` | 🖐 |

## Phase 8 checklist

| # | Check | How | Type |
|---|---|---|---|
| 26 | Containers publish ports on 127.0.0.1 only; no secrets in the image | `pytest tests/test_packaging.py`; CI job `docker` checks `docker compose port` | ✅ |
| 27 | Containers accept only listed container names as "local" (`db`, `api`, `host.docker.internal`), never dotted hostnames | `pytest tests/test_config.py tests/test_netpolicy.py` | ✅ |
| 28 | Packaged mode runs offline once built | Build once online → disconnect → `docker compose --profile app up -d` → sign in, run an investigation | 🖐 |
| 29 | CI is the only part that uses the internet, and it isn't part of the app | `.github/workflows/ci.yml` runs on GitHub's servers only | — |

## Phase 9 checklist

| # | Check | How | Type |
|---|---|---|---|
| 30 | The presentation builds offline | Disconnect → `python scripts/build_presentation.py`; `pytest tests/test_presentation.py` runs under the network guard | ✅ |
| 31 | The deck loads nothing from the internet: no linked images, fonts or videos | Every shape is native (test: no pictures); fonts are Cambria and Calibri, which ship with Office | ✅ |
| 32 | The full demo runs with Wi-Fi off | `docs/demo_script.md`, "Before the audience arrives" | 🖐 |
