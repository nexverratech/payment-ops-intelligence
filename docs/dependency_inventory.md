# Dependency Inventory

Every dependency, why it is here, its licence, and whether it needs an account or the network.
Updated each phase. Exact versions are in `requirements.lock` (generated on the machine where the tests passed).

## Python packages

| Package | Why we need it | Licence | Runs locally | Account / API key | Network at runtime |
|---|---|---|---|---|---|
| fastapi | HTTP API framework | MIT | Yes | No | None. We disable `/docs` because its page loads JavaScript from a public CDN. |
| uvicorn | Runs the API server | BSD-3-Clause | Yes | No | Listens on 127.0.0.1 only |
| pydantic | Validates data and settings | MIT | Yes | No | None |
| pydantic-settings | Loads settings from `.env` | MIT | Yes | No | None |
| sqlalchemy | Parameterized SQL, connection pool | MIT | Yes | No | Only to the local database |
| psycopg (+ binary) | PostgreSQL driver | **LGPL-3.0**. We use it unmodified as a library, which LGPL permits. | Yes | No | Only to the local database |
| httpx2 | Calls the local Ollama API; Starlette's TestClient uses it (replaced the older `httpx`, which Starlette now marks deprecated) | BSD-3-Clause (from the installed package metadata) | Yes | No | Only to 127.0.0.1 (enforced in `config.py`). Its `truststore` dependency reads the Windows certificate store; it makes no network calls itself. |
| pytest | Test runner | MIT | Yes | No | None. Our tests also block all non-local connections. |
| pandas | Building and writing the synthetic tables; analysis | BSD-3-Clause | Yes | No | None |
| numpy | Seeded random numbers, fast array maths | BSD-3-Clause (plus bundled MIT/Zlib/0BSD/CC0 parts) | Yes | No | None |
| streamlit | Dashboard web app | Apache-2.0 | Yes, server on 127.0.0.1:8501 | No | **Sends usage statistics by default**: turned off with `gatherUsageStats = false` in `.streamlit/config.toml` (a test checks this). `headless = true` skips the first-run email prompt. We don't use maps (`st.map`/pydeck), whose base map tiles would come from the internet. |
| langgraph | Agent workflow: shared state, parallel steps, step limit; checkpoints in Phase 6 | MIT | Yes | No | None by itself. Its dependency langchain-core includes the LangSmith client, which only sends traces when `LANGSMITH_TRACING=true` and an API key are set. `.env.example` sets it to `false` (a test checks this), and the test-suite network guard would fail any outbound call. |
| typing_extensions | `TypedDict` that pydantic can validate on Python 3.11 (the workflow state, for checkpoints). Already installed as a pydantic dependency; listed because we now import it directly | PSF-2.0 | Yes | No | None |
| plotly | Chart definitions | MIT | Yes | No | None from Python. In Streamlit, charts are drawn by the plotly.js copy bundled inside Streamlit. We never call `fig.to_html(include_plotlyjs="cdn")`, which would load it from a CDN. |

These licences are the ones each project has long published. PyPI was unreachable from the build workspace, so confirm them against the versions you actually installed (reads local metadata, no network):

```powershell
pip show fastapi uvicorn pydantic pydantic-settings sqlalchemy psycopg httpx2 pytest pandas numpy streamlit plotly langgraph langchain-core | Select-String "^(Name|Version|License)"
```

Re-check whenever you upgrade a major version.

## Non-Python components

| Component | Purpose | Licence / terms | Account | Network behaviour |
|---|---|---|---|---|
| Python 3.11 | Language | PSF | No | None |
| PostgreSQL 16 (Docker image `postgres:16`) | Database | PostgreSQL Licence | No | Image download once; then local only, port bound to 127.0.0.1 |
| Docker Desktop | Runs the database container | **Not open source.** Free under Docker's terms for personal use, education, non-commercial open source and small businesses (<250 employees and <$10M revenue). Podman is a free open-source alternative. | Sign-in optional | Image pulls; Docker Desktop may check for updates |
| Ollama | Local LLM runtime | MIT | No (only for cloud models, which we disable with `OLLAMA_NO_CLOUD=1`) | Model download once. Ollama's docs say local runs don't send prompts to ollama.com. **On Windows it downloads app updates automatically**: when offline this simply doesn't happen. |
| Model `qwen3:4b` / `qwen3:8b` | LLM weights | Apache 2.0 (Qwen team) | No | Download once |

The `langsmith` package (pulled in by LangGraph) registers a pytest plugin. `pyproject.toml` disables it with `-p no:langsmith`, so test runs don't load it.

Streamlit brings its own dependencies (for example pyarrow, altair, tornado, protobuf, GitPython). They are listed with exact versions in `requirements.lock`. GitPython only reads the local `.git` folder; nothing in this project asks it to contact a remote.

## Phase 6: no new dependencies

Checkpoints and history use PostgreSQL through SQLAlchemy and psycopg, which are already installed. LangGraph's own Postgres checkpointer (`langgraph-checkpoint-postgres`, MIT) was considered and not used; the reasons are in `docs/agent_design.md` (Resilience section). One of them is a security advisory for LangGraph's checkpoint deserialization (CVE-2026-28277, fixed in langgraph 1.0.10). This project does not load LangGraph checkpoints, but keep `langgraph` at 1.0.10 or later anyway; `pip show langgraph` shows your version.

## Deliberately not used

| Tool | Reason |
|---|---|
| scikit-learn | The forecast is a 9-parameter linear model; numpy least squares is enough (see `docs/analytics_methods.md`). |
| FAISS / any vector store | No unstructured documents to retrieve in the MVP; SQL serves structured incident data better. |
| LangSmith | Hosted tracing service. `LANGCHAIN_TRACING_V2=false` is set in `.env.example`. |
| langgraph-checkpoint-postgres | Our own validated-JSON step checkpoints instead (see Phase 6 above). |
| Paid LLM APIs | Not allowed by project rules. |
