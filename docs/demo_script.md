# Demo Script

A 6–8 minute live demo that follows the presentation (`presentation/payment_ops_intelligence.pptx`, last slide). Everything runs on your laptop; switch off Wi-Fi before you start to show that it works offline.

## Before the audience arrives (10 minutes)

```powershell
cd "C:\Users\Jaichiddharth R K\payment-ops-intelligence"
.\.venv\Scripts\Activate.ps1
docker compose up -d db
ollama ps                                       # Ollama is running
ollama run qwen3:4b "hi" --think=false          # loads the model into the GPU, so the first answer is fast
```

Then, in two more PowerShell windows (venv activated in each):

```powershell
uvicorn payops.api.main:app --host 127.0.0.1 --port 8000      # window 2
streamlit run src/payops/dashboard/app.py                     # window 3
```

Check:
- `Invoke-RestMethod http://127.0.0.1:8000/health` shows `status: ok` (database, llm and history all `ok`).
- You know the passwords of an **analyst** account (`ana` in the README) and an **admin** account.
- Run one investigation as `ana` beforehand, so the History tab has something in it.
- Switch Wi-Fi off. Reload the dashboard: it still works.

If the `ollama run` flag is not recognised by your Ollama version, run `ollama run qwen3:4b "hi"` instead; it only warms the model.

## The demo

| Time | Do | Say |
|---|---|---|
| 0:00 | Open http://127.0.0.1:8501, sign in as `ana` | "Local accounts, checked by the API. Wi-Fi is off: everything here runs on this laptop." |
| 0:30 | **Trends** and **Providers** tabs | "Failure rate jumps around 23 September. One provider, Kestrel, in orange, stands out. This is plain SQL, no AI yet." |
| 1:30 | **Investigate** tab. Ask: *What went wrong with Kestrel in the week of 21 September 2026?* → **Run investigation** | "Six agents now work: the LLM plans, three agents analyse in parallel, impact is sized, the LLM writes, and every sentence is checked." |
| 2:00 | While it runs (about 20–30 s) | "The model never computes a number. It only plans and phrases." |
| 2:30 | Read the headline; open **Hypotheses (ranked by evidence, none proven)** | "Kestrel's failure rate went from about 3 % to 23 % in the window. Our own deployment two hours earlier is ranked *unlikely*: only Kestrel got worse, and our software serves all four providers." |
| 3:30 | Open the **Verification** and **Findings and evidence** expanders; match one sentence's grey ID tags to its finding | "Every sentence cites findings; numbers, dates and causal wording are checked; key figures are recomputed from the database. Removed sentences are listed with the reason." |
| 4:30 | **History** tab: open an earlier run; point at *Resume from checkpoints* | "Each step is checkpointed. An interrupted run resumes and reuses the finished steps." |
| 5:15 | Sign out. Sign in as the **admin** | "Separation of duties: the admin has no Investigate tab, and the API refuses the request even if someone calls it directly." |
| 5:45 | **Audit log** tab → **Verify the hash chain** | "Every sign-in, investigation and refusal is recorded. The log is append-only, and the hash chain shows if any row was changed." |
| 6:30 | Back to the slides | |

## If something goes wrong

| Problem | Fallback |
|---|---|
| The LLM is slow or Ollama is not running | The report is a clearly labelled template report; say so, it is the designed fallback. Or in PowerShell: `python -m payops.agents.run "Why did the failure rate increase last week?" --no-llm` (under a second) |
| The dashboard won't load | Use the CLI: `python -m payops.agents.run "What went wrong with Kestrel in the week of 21 September 2026?"` |
| Port 8000 or 8501 already in use | The Docker containers are running: `docker compose --profile app down`, keep `db` running |
| Sign-in says the account is locked | `python -m payops.security.manage unlock ana` |
| The database is down | `docker compose up -d db`, wait 10 seconds, reload |

## Questions you may be asked

| Question | Short answer |
|---|---|
| Why not use GPT-4 or Claude through an API? | A project rule: no paid APIs, no data leaving the computer. The design (code computes, the LLM writes, everything is checked) is what makes a 4-billion-parameter local model usable. |
| How do you know it found the right cause? | The data has an answer key that the app never reads. The tests and `docs/evaluation.md` compare the output with it. |
| What stops the LLM from inventing numbers? | It never computes them, and every number it writes must appear in the finding it cites, or the sentence is removed. |
| What if someone types a prompt injection? | The question is data inside tags, the plan is validated by code, and the LLM has no tools. Both injection questions in the evaluation were handled safely. |
| Could it be wrong anyway? | Yes: a correct number can be given the wrong meaning. That's why the evidence is shown next to every sentence, and why human review of a sample is the next step (`docs/limitations_and_future_work.md`). |
| Why six agents instead of one script? | The separation gives each step narrow permissions, its own evidence and its own checkpoint. `docs/agent_design.md` also says where a plain program is the better choice. |
