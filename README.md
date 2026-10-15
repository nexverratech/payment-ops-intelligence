# Payment Operations Intelligence

A local-only, multi-agent AI system that helps payment operations teams investigate payment failures. Everything runs on your own computer with synthetic data. No paid APIs, no cloud services, no accounts.

> **Status: Phase 1 (environment and skeleton).** The app currently has a configuration system, structured logging, a PostgreSQL database in Docker, and a `GET /health` endpoint. Data, analytics, agents and the dashboard arrive in later phases.

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
```

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

Open `.env` in an editor and replace `POSTGRES_PASSWORD` with the random string you just printed. `.env` is ignored by Git.

### 2.4 Database (PostgreSQL in Docker)

Make sure Docker Desktop is running, then:

```powershell
docker compose up -d db
docker compose ps
```

Expected: `payops-db` with status `Up ... (healthy)` after 10–20 seconds. The first run downloads the `postgres:16` image (internet needed once).

> If you change `POSTGRES_PASSWORD` **after** the first start, the database keeps the old password. Reset it with `docker compose down -v` (this deletes the database data) and start again.

## 3. Run the tests

```powershell
pytest
```

Expected with the database running: **37 passed**. If the database is stopped, the 2 integration tests are reported as *skipped*, not failed.

After the tests pass, record the exact versions that worked. This file is the project's pinned dependency list:

```powershell
pip freeze | Out-File -Encoding utf8 requirements.lock
```

From then on, a fresh install uses `pip install -r requirements.lock`.

## 4. Start the API

```powershell
uvicorn payops.api.main:app --app-dir src --host 127.0.0.1 --port 8000
```

In a **second** PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Depth 5
```

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

## 5. Save your work in Git

```powershell
git init
git add .
git status
```

Check that `.env` is **not** in the list of files to be committed. Then:

```powershell
git commit -m "Phase 1: environment, configuration, health endpoint"
```

## 6. Offline check (Phase 1 part)

1. Turn off Wi-Fi / unplug the network cable.
2. Run `pytest` and the health check again.
3. Both must give the same results as before. Turn the network back on afterwards.

## 7. Shutdown

1. Press `Ctrl+C` in the uvicorn window.
2. `docker compose stop db` (keeps your data).
3. `deactivate` to leave the virtual environment.
4. Ollama can stay running, or quit it from the tray icon.

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| `py : The term 'py' is not recognized` | Reinstall Python 3.11 from python.org and tick "Add python.exe to PATH" and the "py launcher" option. |
| `ValidationError ... postgres_password Field required` | `.env` is missing or you are not in the project folder. Run `Copy-Item .env.example .env`. |
| `POSTGRES_HOST ... is not local` | The app only allows `127.0.0.1` or `localhost`. This is deliberate. |
| `docker compose` says it can't connect to the Docker engine | Start Docker Desktop and wait until it says "Engine running". |
| Port 5432 already in use | Another PostgreSQL is installed. Set `POSTGRES_PORT=5433` in `.env`, then `docker compose up -d db`. |
| Health shows `database: unreachable` with `OperationalError` | Container not healthy yet, or password changed after first start (see 2.4). |
| Health shows `llm: model_missing` | Run `ollama pull qwen3:4b`, or change `OLLAMA_MODEL` in `.env` to a model shown by `ollama list`. |
| `ollama ps` shows `100% CPU` on an AMD GPU | Ollama's docs say AMD GPUs not on the ROCm list use the Vulkan backend. Set the user variable `OLLAMA_VULKAN=1`, restart Ollama, and check `server.log`. If it picks the integrated (APU) GPU and is unstable, set `GGML_VK_VISIBLE_DEVICES` to your discrete GPU's number. CPU-only still works, just slower. |

## Project documentation

- `docs/file_guide.md`: what every file does
- `docs/dependency_inventory.md`: every dependency, its licence, and its network behaviour
- `docs/offline_checklist.md`: how to prove the app works offline
