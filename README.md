# Payment Operations Intelligence

A local-only, multi-agent AI system that helps payment operations teams investigate payment failures. Everything runs on your own computer with synthetic data. No paid APIs, no cloud services, no accounts.

> **Status: complete (Phases 0–9).** Presentation: `presentation/payment_ops_intelligence.pptx` (section 10). Built in phases: synthetic data with an injected incident, PostgreSQL with separate read-only and history logins, tested analytics, a FastAPI service, a Streamlit dashboard, and a LangGraph workflow: six agents, permission-checked tools, evidence on every finding, ranked hypotheses, a local LLM that plans and writes (never calculates), and a Verification agent that checks every sentence. Every investigation is saved with a checkpoint after each step; an interrupted or partial one can be resumed. Time, tool-call, LLM-call and concurrency limits keep a run bounded. Without the LLM you get a clearly labelled template report. Local sign-in with four roles is enforced by the backend on every request, and every action is written to an append-only, hash-chained audit log (`docs/security.md`). The real model's planning accuracy, verification pass rate and speed are measured on fixed questions (`docs/evaluation.md`). It runs packaged in Docker; a GitHub Actions workflow is written but not yet run on GitHub.

---

## 1. Prerequisites (Windows 11)

| Tool | Version | Free? | Account needed? | Check it with |
|---|---|---|---|---|
| Python | 3.11 | Yes (PSF licence) | No | `py -3.11 --version` |
| Docker Desktop | any recent | Free for personal use, education, small business | No (sign-in optional) | `docker version` |
| Git | any | Yes (GPL-2.0) | No | `git --version` |
| Ollama | current | Yes (MIT) | No (only for cloud models, which we disable) | `ollama --version` |

Hardware: 16 GB RAM is enough for `qwen3:4b`. See `docs/dependency_inventory.md` for every package, its licence and its network behaviour.

## 2. One-time setup

All commands are for **PowerShell**, run from the project folder (for example `C:\dev\payment-ops-intelligence`).

### 2.1 Ollama (local LLM) in local-only mode

1. Install Ollama from <https://ollama.com/download> (`OllamaSetup.exe`).
2. Turn off Ollama's cloud features for your Windows user:
   ```powershell
   [Environment]::SetEnvironmentVariable("OLLAMA_NO_CLOUD", "1", "User")
   ```
3. Quit Ollama (right-click its tray icon → Quit), then start it again from the Start menu so it sees the new setting.
4. Download the model (about 2.5 GB; this is the only time Ollama needs the internet):
   ```powershell
   ollama pull qwen3:4b
   ollama list
   ```
   Expected: `qwen3:4b` appears in the list.
5. Confirm local-only mode: open `%LOCALAPPDATA%\Ollama\server.log` and look for `Ollama cloud disabled: true`.
6. Check whether the GPU is used:
   ```powershell
   ollama run qwen3:4b "Reply with the word ready"
   ollama ps
   ```
   The `PROCESSOR` column shows `GPU`, `CPU`, or a split. CPU is slower but works. See Troubleshooting for AMD GPUs.

### 2.2 Python environment

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.in
pip install -e . --no-deps
```

The last line registers this project's own `src/payops` package with the venv, so commands like `python -m payops.data.generator` work.

If activation fails with *"running scripts is disabled on this system"*, run this once, then activate again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Linux/macOS equivalent: `python3.11 -m venv .venv && source .venv/bin/activate`.

### 2.3 Environment variables

```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

Open `.env` in an editor and replace `POSTGRES_PASSWORD` with the random string you just printed. Run the `python -c ...` line again and put the second random string in `READONLY_DB_PASSWORD`, and a third in `APP_DB_PASSWORD` (three different passwords). `.env` is ignored by Git.

### 2.4 Database (PostgreSQL in Docker)

Make sure Docker Desktop is running, then:

```powershell
docker compose up -d db
docker compose ps
```

Expected: `payops-db` with status `Up ... (healthy)` after 10–20 seconds. The first run downloads the `postgres:16` image (internet needed once).

> If you change `POSTGRES_PASSWORD` **after** the first start, the database keeps the old password. Reset it with `docker compose down -v` (this deletes the database data) and start again.

## 3. Generate and load the synthetic data

```powershell
python -m payops.data.generator --seed 42 --out data/generated
python -m payops.db.loader --data-dir data/generated
```

The generator takes 10–30 seconds and prints the dataset ID, about 490,000 transactions and a failure rate near 2.9%. The exact numbers can differ slightly between NumPy versions; on one machine, the same seed always gives byte-identical files. The loader takes a similar time and prints the row count of every table.

Check that the injected incident is visible, using plain SQL:

```powershell
Get-Content sql\validate_incident.sql | docker exec -i payops-db psql -U payops_admin -d payops
```

Expected: the weekly failure rate jumps in the week of 21 Sep; `KESTREL` has the largest increase; its top failure reason last week is `TIMEOUT`; the 6-hour blocks show the problem from 23 Sep 12:00 to 25 Sep 00:00; the `checkout-service` deployment is followed by a jump, the `fraud-rules` deployment is not. Compare with `data/generated/ground_truth.json`, the answer key. The application never reads it.

### Accounts, history and audit log (once)

```powershell
python -m payops.db.setup_app
python -m payops.security.manage create-user admin --role admin
python -m payops.security.manage create-user ana --role analyst
```

`setup_app` creates schema `ops` (investigations and checkpoints, accounts, sessions, audit log) and the login `APP_DB_USER`, which may use that schema and nothing else. It is safe to run again, and reloading the payment data doesn't touch it. `create-user` asks for the password twice: at least 12 characters, and a passphrase is fine. Roles: `viewer`, `analyst`, `lead`, `admin` (table in `docs/security.md`). The API and dashboard need a signed-in user; without `APP_DB_PASSWORD` they refuse every request except `/health`.

### Run the deterministic analytics

```powershell
python -m payops.analytics.report
python -m payops.analytics.detection_eval
```

The report reviews the last complete week: the overall change, which provider explains it, anomaly windows, nearby deployments and provider notices, impact (each figure labelled measured / estimate / hypothetical), and a 7-day forecast. No LLM is involved. The second command measures detection precision and recall over 8 seeds (about 10–30 seconds).

### Run the agent workflow

With Ollama running:

```powershell
python -m payops.agents.run "What went wrong with Kestrel in the week of 21 September 2026?"
```

The local LLM reads the question, the agents compute the findings, the LLM writes the report, and the Verification agent checks every sentence. You get the verified report and then the **agent trace**: steps, tools, LLM calls, timings. Writing the report takes about 10–60 s on a laptop GPU.

- `--no-llm`: skip the LLM. You get a deterministic plan and the labelled template report, in under a second.
- `--json`: print the full structured result, including every tool call and verification check.
- `--list`: saved investigations, newest first. `--show <id>`: print one again.
- `--resume <id>`: continue a **partial** or **interrupted** investigation. Steps that already succeeded are reused from their checkpoints (marked `*` in the trace); the rest run again. Press Ctrl+C during a run to try it: the run is recorded as interrupted and the command prints the resume line.

What to do when something goes wrong (database or Ollama down, a run stuck or interrupted, "busy" answers): `docs/runbook.md`.

If you see "did not answer in time", raise `LLM_TIMEOUT_SECONDS` in `.env` (default 180). See `docs/agent_design.md`.

## 4. Run the tests

```powershell
pytest
```

Expected with the database running, data loaded, `setup_app` done, Ollama running and `requirements-docs.txt` installed (section 10): **441 passed**. Without python-pptx, the presentation test file is skipped (**435 passed, 1 skipped**). Without Ollama, the one live-LLM test is skipped too. Without `APP_DB_PASSWORD` or `setup_app`, the 11 live history and sign-in tests are skipped. If the database is stopped or no data is loaded, all 50 integration tests are skipped, not failed.

Measure the real model on 10 questions with known answers (about 3–5 minutes):

```powershell
python -m payops.evaluation.llm_eval
```

It prints a table, and saves JSON and Markdown under `evaluation/results/`. What each number means: `docs/evaluation.md`.

The same suite runs on GitHub on every push (section 6b). To see what the real model wrote: `pytest -s tests/integration/test_llm_live.py`.

After the tests pass, record the exact versions that worked. This file is the project's pinned dependency list:

```powershell
pip freeze --exclude-editable | Out-File -Encoding ascii requirements.lock
```

From then on, a fresh install uses `pip install -r requirements.lock`.

## 5. Start the API and the dashboard

You need **two** PowerShell windows, both in the project folder with the venv activated.

**Window 1, the API:**

```powershell
uvicorn payops.api.main:app --host 127.0.0.1 --port 8000
```

**Window 2, the dashboard:**

```powershell
streamlit run src/payops/dashboard/app.py
```

Then open <http://127.0.0.1:8501> in your browser and sign in with an account you created. Streamlit doesn't open the page for you, because `.streamlit/config.toml` runs it headless; that also skips Streamlit's first-run email prompt. The tabs follow your role: viewers see the analytics, analysts and leads also see **Investigate** and **History**, and admins see **History** and the **Audit log**.

What you'll see:
- KPI tiles: attempts, succeeded, failed, failure rate, affected transaction value. Each is compared with the period of equal length before.
- **Trends**: failure rate per provider, with deployments (dotted lines) and provider notices (shaded spans), plus volume.
- **Providers**: extra failures per provider and the full comparison table.
- **Failure reasons**: this period vs the previous one.
- **Incidents & timeline**: detected anomaly windows with nearby events.
- **Investigate**: ask a question. Phase 4 returns the deterministic weekly review, clearly labelled.

Filters (date range, provider, method, reason) are in the sidebar, together with the system status.

### The API on its own

With uvicorn running, try the API directly from another window:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Depth 5
$cred = @{ username = "ana"; password = Read-Host "Password" } | ConvertTo-Json
$token = (Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/v1/auth/login -ContentType "application/json" -Body $cred).token
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/kpis?provider=KESTREL" -Headers @{ Authorization = "Bearer $token" } | ConvertTo-Json -Depth 5
```

`/health` is the only data-free endpoint that needs no sign-in; without the `Authorization` header, `/api/v1/kpis` answers 401.

Expected (with Ollama running and the model pulled):

```json
{
  "status": "ok",
  "version": "0.1.0",
  "environment": "dev",
  "llm_mode": "ollama",
  "components": {
    "database": { "status": "ok", "latency_ms": 12.3, "detail": null },
    "llm":      { "status": "ok", "latency_ms": 4.1,  "detail": null }
  }
}
```

| What you see | Meaning |
|---|---|
| `status: ok` | Database and LLM are both reachable |
| `status: degraded`, `llm_mode: fallback` | Database fine; Ollama off or model not pulled. The app still works with labelled template output (later phases). |
| HTTP 503, `status: down` | Database unreachable. Run `docker compose ps`. |

## 6. Save your work in Git

```powershell
git init
git add .
git status
```

Check that `.env` is **not** in the list of files to be committed. Then:

```powershell
git commit -m "Phase 1: environment, configuration, health endpoint"
```

## 6a. Run everything in Docker (packaged mode)

The same application as a container image: database, API and dashboard, all started with one command. Use it to show the project on another machine or to check that it doesn't depend on your laptop's setup. Daily development stays in the venv (sections 3–5).

**Before starting:** stop the uvicorn and Streamlit windows (Ctrl+C), because the containers use the same ports 8000 and 8501.

```powershell
docker compose --profile app up -d --build
docker compose --profile app ps
```

- **First time:** the build downloads the Python base image and the packages in `requirements.lock`, about 5–10 minutes. Afterwards it's cached.
- **Running:** `ps` should list `db`, `api` and `dashboard` as running, and `setup` as exited (0). `setup` is the one-time database setup; finishing is its job.
- **Then:** open http://127.0.0.1:8501 and sign in as before. Your data, accounts and history are the same, because the containers use the same database.

| Packaged mode differs in | Why |
|---|---|
| The LLM is off by default (labelled template reports) | Ollama on Windows listens on 127.0.0.1 only, which containers can't reach. The opt-in, with its trade-off, is in `docs/runbook.md` → Docker |
| The API container never gets the owner password; the dashboard container gets no database password at all | Least privilege: each container holds only the logins it uses |

**On a fresh machine** (empty database), generate and load the data inside containers first:

```powershell
docker compose up -d db
docker compose --profile tools run --rm tools python -m payops.data.generator --seed 42 --out data/generated
docker compose --profile tools run --rm tools python -m payops.db.loader --data-dir data/generated
docker compose --profile app up -d --build
docker compose --profile tools run --rm tools python -m payops.security.manage create-user admin --role admin
```

**Stop:** `docker compose --profile app down` (keeps the data).

## 6b. Continuous integration on GitHub

`.github/workflows/ci.yml` runs three jobs on GitHub's servers on every push:

| Job | What it does |
|---|---|
| **tests** | Python 3.11 and PostgreSQL 16; generates and loads the seed-42 data; runs the whole suite. It fails if any database test was skipped instead of run |
| **docker** | Builds the image, loads data in containers, signs in, runs an investigation through the API, and checks containers run as non-root with ports on 127.0.0.1 only |
| **dependency-audit** | `pip-audit` checks every pinned package for known vulnerabilities |

CI uses only synthetic data and throwaway passwords made for each run, so the repository needs no secrets.

To use it, put the project on GitHub:
1. On github.com, create an empty repository, private or public. Don't add a README.
2. Check that no secret is tracked. This should print `.env.example` and nothing else:

   ```powershell
   git ls-files | Select-String "\.env"
   ```

3. Push:

   ```powershell
   git remote add origin https://github.com/<your-user>/payment-ops-intelligence.git
   git push -u origin master
   ```

Git for Windows opens a browser window to sign in to GitHub the first time. Then open the repository's **Actions** tab to watch the run.

## 7. Offline check

1. Turn off Wi-Fi / unplug the network cable.
2. Run the generator, the loader, `pytest` and the health check again.
3. Both must give the same results as before. Turn the network back on afterwards.

## 8. Shutdown

1. Press `Ctrl+C` in the Streamlit window and in the uvicorn window.
2. `docker compose stop db` (keeps your data).
3. `deactivate` to leave the virtual environment.
4. Ollama can stay running, or quit it from the tray icon.

## 9. Troubleshooting

| Problem | Fix |
|---|---|
| `py : The term 'py' is not recognized` | Reinstall Python 3.11 from python.org and tick "Add python.exe to PATH" and the "py launcher" option. |
| `POSTGRES_PASSWORD is not set. This command needs the database owner login` | `.env` is missing or you are not in the project folder. Run `Copy-Item .env.example .env` and fill it in. |
| `POSTGRES_HOST ... is not local` | The app only allows `127.0.0.1` or `localhost`. This is deliberate. |
| `docker compose` says it can't connect to the Docker engine | Start Docker Desktop and wait until it says "Engine running". |
| Port 5432 already in use | Another PostgreSQL is installed. Set `POSTGRES_PORT=5433` in `.env`, then `docker compose up -d db`. |
| Health shows `database: unreachable` with `OperationalError` | Container not healthy yet, or password changed after first start (see 2.4). |
| `READONLY_DB_PASSWORD is not set` | Add `READONLY_DB_USER` and `READONLY_DB_PASSWORD` to `.env` (see `.env.example`). |
| `docker compose ... up` fails: port 8000 or 8501 is already allocated | The venv API or dashboard is still running. Press Ctrl+C in those windows first |
| Docker build stops with "core packages missing" | `requirements.lock` is empty or unreadable. Regenerate it in the activated venv: `pip freeze --exclude-editable \| Out-File -Encoding ascii requirements.lock` (`>` in Windows PowerShell writes UTF-16; the build reads that too, but ASCII keeps Git diffs readable) |
| Docker build stops at `pip install` naming a package that "has no matching distribution" | A Windows-only package in the lock. Add its name to `WINDOWS_ONLY` in `scripts/prepare_requirements.py` |
| `No module named payops` | Run `pip install -e . --no-deps` inside the activated venv. |
| `LOAD FAILED: ... does not match its manifest fingerprint` | A CSV was edited or partly copied. Re-run the generator. |
| Integration tests fail with `password authentication failed for user "payops_readonly"` | You changed `READONLY_DB_PASSWORD` after loading. Re-run the loader: it resets the password. |
| Dashboard says "Cannot reach the API" | Start uvicorn in another window (section 5). |
| `streamlit : The term 'streamlit' is not recognized` | The venv isn't active in that window: run `.\.venv\Scripts\Activate.ps1`. |
| Dashboard page is blank or errors after an update | Press `C` in the browser to clear Streamlit's cache, or restart Streamlit. |
| Report says "Template report used: local LLM unavailable ... did not answer in time" | Increase `LLM_TIMEOUT_SECONDS` in `.env`, or use a smaller model. |
| Report says "verification removed N of M LLM sentences" | The model wrote claims that didn't match the findings, so you got the template instead. That's the safety net working. See the Verification section for which sentences and why. |
| Health shows `llm: model_missing` | Run `ollama pull qwen3:4b`, or change `OLLAMA_MODEL` in `.env` to a model shown by `ollama list`. |
| `ollama ps` shows `100% CPU` on an AMD GPU | Ollama's docs say AMD GPUs not on the ROCm list use the Vulkan backend. Set the user variable `OLLAMA_VULKAN=1`, restart Ollama, and check `server.log`. If it picks the integrated (APU) GPU and is unstable, set `GGML_VK_VISIBLE_DEVICES` to your discrete GPU's number. CPU-only still works, just slower. |

## 10. The presentation

The deck is generated by a script, so its numbers and its speaker notes stay in step with the docs.

```powershell
pip install -r requirements-docs.txt        # python-pptx: MIT licence, local, no account, no network
python scripts/build_presentation.py
```

Expected output: `Wrote presentation\payment_ops_intelligence.pptx (16 slides) and docs\presentation_notes.md`. Open the `.pptx` in PowerPoint (or LibreOffice Impress, free): every text box, shape, table and chart is editable, and each slide has speaker notes. To change a number, edit `scripts/build_presentation.py` and rebuild; `pytest tests/test_presentation.py` fails if `docs/presentation_notes.md` is out of date. The live demo that goes with it: `docs/demo_script.md`.

## Project documentation

- `docs/business_problem.md`: the problem in plain English, and the hidden test scenario
- `docs/architecture.md`: components, request path, data boundaries, trade-offs (diagram: `docs/architecture.svg`)
- `docs/agent_design.md`: the six agents, hypothesis ranking, verification, resilience
- `docs/limitations_and_future_work.md`: what the measurements can't tell you, and what's next
- `docs/demo_script.md`: a timed live demo with fallbacks
- `docs/presentation_notes.md`: speaker notes for the deck (generated)

- `docs/file_guide.md`: what every file does
- `docs/data_dictionary.md`: every table and column, and the injected scenario
- `docs/analytics_methods.md`: every metric and method in plain English
- `docs/evaluation.md`: measured results (detection precision/recall, report vs ground truth)
- `docs/dependency_inventory.md`: every dependency, its licence, and its network behaviour
- `docs/offline_checklist.md`: how to prove the app works offline
- `docs/security.md`: accounts, roles, audit log, threat model
- `docs/runbook.md`: what to do when something goes wrong, including Docker
