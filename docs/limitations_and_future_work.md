# Limitations and Future Work

What this project does not do, what its measurements cannot tell you, and what would come next. Each item says where the evidence is.

## Limitations of the evidence

| Limitation | Why it matters | Evidence |
|---|---|---|
| **Synthetic data.** Incidents are cleaner than real ones: one provider, one reason, sharp start and end. | Real-world detection precision and recall would be lower than measured. | `docs/data_dictionary.md`, `docs/evaluation.md` |
| **Detection thresholds were tuned on seed 42.** | Seeds 1–7 were not used for tuning (precision 12/12, recall 12/14 on those alone), but 8 seeds is a small sample. | `docs/evaluation.md` |
| **The LLM was measured on 10 questions, twice.** | 93.8 % of sentences kept and 10/10 plans are indicative, not precise. | `docs/evaluation.md`, runs 1 and 2 |
| **One model on one laptop.** `qwen3:4b` on a 16 GB laptop with an AMD GPU. | Latency (median 26 s, p90 57 s) and quality will differ on other hardware and models. | `docs/evaluation.md` |
| **CI has not run on GitHub yet.** The workflow is written and its structure is tested locally. | Nobody has seen the three jobs pass on GitHub's machines. | `.github/workflows/ci.yml`, `tests/test_packaging.py` |

## Limitations of the system

| Limitation | Effect | Possible fix |
|---|---|---|
| **Right numbers, wrong meaning.** Verification checks that each number appears in the cited finding, not that it keeps its meaning (example: a cost range for the incident so far described as the next 24 hours). | A kept sentence can still mislead. Findings are shown beside every sentence so a reader can check. | Human review of a sample of reports to measure how often; finer-grained findings (one figure per finding) |
| **Wrong citations to right numbers** are the most common remaining LLM error. | They are removed, so reports get shorter. | A larger model; citation hints in the data block |
| **Unknown provider names** in a question ("PayFast") are not flagged; the report quietly covers all providers. | The user may think the named provider was analysed. | A deterministic check of names in the question against known providers |
| **Questions are about one week.** The planner picks a week and a comparison week. | "Compare July with August" is answered for a week. | More plan shapes, validated the same way |
| **No streaming.** The dashboard waits for the whole run (about 20–60 s). | Feels slow. | Server-sent progress per step, which the checkpoints already make possible |
| **Containers use the template report by default.** Ollama listens on 127.0.0.1, which containers can't reach. | Packaged mode shows no LLM report unless you opt in. | Documented opt-in with its exposure (`docs/runbook.md`); a firewall rule limited to Docker's network (not verified) |
| **Every analyst sees all providers.** | Fine for one team; not for a provider-specific team. | Per-provider data scoping in the tools layer |
| **Audit chain: the newest rows can be removed** without breaking the chain. | Detected only by comparing the latest hash noted outside. | Ship audit events to a separate write-once store |
| **Local accounts only.** No MFA or single sign-on. | Acceptable for a single computer. | An organisation's identity provider, if the app ever left this computer |
| **No real provider integrations.** | Provider notices and deployments come from the synthetic data. | Read-only importers for status pages and CI/CD history, still running locally |

## Future work, in order

1. **Larger model on the new PC** (RTX 5070 Ti, 32 GB RAM): `qwen3:8b` and `qwen3:14b`, with the same `python -m payops.evaluation.llm_eval` command and questions so results compare directly with runs 1 and 2.
2. **Human review sample:** read 20 kept sentences per model and count meaning errors. This is the one error class code cannot measure.
3. **Unknown-name check** for providers in the question (small, deterministic, testable).
4. **Push to GitHub** and get `tests`, `docker` and `dependency-audit` green; pin the base image by digest.
5. **Per-provider scoping** for analyst accounts.
6. **Progress streaming** to the dashboard.
7. **More realistic incidents** in the generator: gradual degradations, two overlapping causes, partial recoveries; then re-measure detection.

## What would change for real payment data

Real data would need more than code changes: a data-protection review, pseudonymised customer identifiers, retention rules for history and the audit log, TLS and a reverse proxy if the app ever left 127.0.0.1, secrets in a vault, and an organisation's identity provider with MFA (`docs/security.md`, "What a production deployment would add").
