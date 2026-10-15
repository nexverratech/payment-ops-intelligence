# Runbook

What to do when something goes wrong. All commands are Windows PowerShell, run in the project folder with the venv activated (`.\.venv\Scripts\Activate.ps1`).

## Daily start and stop

```powershell
docker compose up -d db                                         # database
uvicorn payops.api.main:app --host 127.0.0.1 --port 8000        # window 1: API
streamlit run src/payops/dashboard/app.py                       # window 2: dashboard
```

Ollama starts with Windows; check it with `ollama ps`. Stop the API and dashboard with Ctrl+C, and the database with `docker compose stop`. The data and history stay.

## First check: health

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Depth 4
```

| Component | Status | Meaning | What to do |
|---|---|---|---|
| database | `unreachable` | The API can't reach PostgreSQL; overall `down`, HTTP 503 | `docker compose ps`; start Docker Desktop, then `docker compose up -d db` |
| llm | `unreachable` / `model_missing` | Ollama is off or the model isn't pulled; overall `degraded` | Start Ollama, or `ollama pull qwen3:4b`. Meanwhile investigations give the labelled template report |
| history | `not_configured` | `APP_DB_PASSWORD` is empty; investigations are not saved | Set it in `.env`, then `python -m payops.db.setup_app` |
| history | `not_set_up` | The login works but the tables or permissions are missing | `python -m payops.db.setup_app` |
| history | `unreachable` | Wrong password, or the database is down | Check `APP_DB_PASSWORD`; check the database row above |

## An investigation was interrupted or is partial

**Symptoms:**
- The History tab or `--list` shows `interrupted` (stopped mid-run) or `partial` (some steps failed or ran out of time).
- A run that seems stuck shows `running`.

**Why a run shows `interrupted`:**
- Ctrl+C or a crash inside the workflow is recorded immediately.
- If the whole process died (window closed, power cut, PC restarted), the run stays `running` until the heartbeat check notices it. After every step the run refreshes its `updated_at` time. A run with no refresh for longer than the time budget plus two report attempts plus a minute (`stale_after_seconds`: 600 + 2 × 180 + 60 s = 17 minutes with the defaults) is marked `interrupted` the next time history is listed or opened.

**Resume:**

```powershell
python -m payops.agents.run --list
python -m payops.agents.run --resume <id>
```

In the dashboard, open the **History** tab, pick the investigation and click **Resume from checkpoints**.

**What a resume does:**
- Steps that already succeeded are reused, provided every step they depend on is reused too. They are marked `*` in the trace and "from checkpoint" in the dashboard.
- Everything else runs again, with a fresh time budget and a fresh LLM call budget.
- If the report was written without the LLM because the LLM was down, it is written again.

**When resume is refused (HTTP 409):**

| Message | Why | What to do |
|---|---|---|
| "complete; there is nothing to resume" | Only partial or interrupted runs can resume | Open it with `--show <id>`, or start a new investigation |
| "already running" | Another resume or run holds it; a status switch in one SQL statement prevents two at once | Wait. If it's truly dead, it turns `interrupted` after the heartbeat limit |
| "data was reloaded" | Checkpoints were computed on a different dataset; mixing them would give wrong answers | Start a new investigation |

## "N investigations are already running" (HTTP 429)

At most `MAX_CONCURRENT_INVESTIGATIONS` (default 2) run at once, because they share one local GPU. Wait for one to finish, or raise the limit in `.env` and restart the API. On the 16 GB laptop, keep it at 1–2.

## An investigation ran out of time

The report says "time limit for the investigation reached" for the skipped steps, and the status is `partial`. Then:
- **Resume it.** It continues with a fresh budget.
- **Raise the limit.** Set `INVESTIGATION_TIMEOUT_SECONDS` in `.env` (default 600), then restart the API.
- **Check the time split.** If the LLM is the slow part, look at the `llm:report` row in the trace. The time limit is checked before each step; one LLM reply is bounded by `LLM_TIMEOUT_SECONDS`.

## The database hiccups during a run

Each analytics query that loses its connection is retried once after half a second. The tool call then shows `attempts: 2`. A query slower than 30 s is stopped by the read-only login's `statement_timeout` and is not retried. The step fails, the investigation is `partial`, and you can resume it.

If the **history** database is unavailable during a run, the investigation still finishes and answers, with `persisted: false`. It just can't be listed or resumed later.

## Reloading the data

```powershell
python -m payops.db.loader --data-dir data/generated
```

This rebuilds schema `payments` only; saved history in schema `ops` is kept. Old investigations stay readable. Resuming them is refused (see above), because their checkpoints belong to the previous dataset.

## Logs

The API logs one JSON line per event to the console, without question text, passwords or SQL. Useful events:
- `investigation completed`: id, status, mode, duration, run number, persisted
- `investigation resumed`: reused steps
- `agent failed`: step name and error type
- `history write failed`: the history database was unavailable

## Who may do what in the database

| Login | Can | Cannot |
|---|---|---|
| `POSTGRES_USER` (owner) | Everything; used only by the loader, `setup_app` and health checks | — |
| `payops_readonly` | SELECT on `payments.*`; every session read-only; 30 s per query | Write anything; see `ops.*` |
| `payops_app` (application) | SELECT/INSERT/UPDATE on investigations, steps, users, sessions; SELECT/INSERT on the audit log; 10 s per query | See `payments.*`; DELETE anything; UPDATE the audit log; change tables |

Deleting old history is an owner task:

```powershell
docker exec -it payops-db psql -U payops_admin -d payops -c "DELETE FROM ops.investigations WHERE created_at < now() - interval '90 days'"
```

The steps are removed with their investigation (`ON DELETE CASCADE`).

## Accounts and sign-in

| Situation | What to do |
|---|---|
| First start, no accounts | `python -m payops.security.manage create-user admin --role admin` |
| "Invalid username or password" | Check the spelling (usernames are lowercase). Repeated failures lock the account |
| "Account temporarily locked" | Wait `LOGIN_LOCKOUT_MINUTES` (15), or `python -m payops.security.manage unlock <name>` |
| Forgotten password | `python -m payops.security.manage reset-password <name>`; it ends their sessions |
| Someone leaves | `python -m payops.security.manage disable <name>`; their sessions end on their next request |
| Change what someone may do | `python -m payops.security.manage set-role <name> lead`; they sign in again with the new rights |
| Dashboard returns to the sign-in page | The session expired: 30 minutes idle or 8 hours after sign-in, by design |
| API answers 503 "Sign-in is not configured" | `APP_DB_PASSWORD` is missing, or `setup_app` hasn't run |
| Last admin can't be disabled or demoted | Create another admin first; this guard prevents a lock-out |

`list-users` shows every account and its lock state.

## Audit log

```powershell
python -m payops.security.manage audit --limit 30     # newest events
python -m payops.security.manage verify-audit         # recompute the hash chain
```

`verify-audit` prints the latest hash. Note it somewhere outside the database: comparing it later is how you would notice that the newest rows were removed, the one change the chain alone can't show.

If verification reports a broken row, the log was edited outside the application, which needs the owner login and disabling a trigger. Treat it as a security incident:
1. Keep the database as it is.
2. Compare the row with backups.
3. Check who has the `POSTGRES_USER` password.

## Measuring the model

```powershell
python -m payops.evaluation.llm_eval            # 10 questions, ~3-5 minutes with qwen3:4b
python -m payops.evaluation.llm_eval --cases 7 8   # only the injection questions
```

Results go to `evaluation/results/`. Run it after changing the model (`OLLAMA_MODEL`) or the prompts, and compare.
