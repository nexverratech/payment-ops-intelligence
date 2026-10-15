# Data Dictionary

All data is **synthetic**. Companies, customers, amounts and incidents are invented by `payops.data.generator`. There is no real person, card number or account anywhere.

## How the tables relate

```
providers (provider_code PK)
   ▲            ▲                 ▲
   │            │                 │
   │   provider_incidents         │
   │   (incident_id PK,           │
   │    provider_code FK) ◄───────┼──── transactions.provider_event_id (FK, optional)
   │                              │
transactions (transaction_id PK, provider_code FK)
   │
   └── summarised into ──► daily_stats (stat_date + provider_code + payment_method = PK)

deployments (deployment_id PK)          no foreign keys: linked to failures only by TIME
dataset_runs (dataset_id PK)            which generated dataset is loaded
```

- A **primary key (PK)** is the column (or set of columns) that uniquely identifies a row. Example: no two payments share a `transaction_id`.
- A **foreign key (FK)** is a column that must match a primary key in another table. Example: every `transactions.provider_code` must exist in `providers`. The database rejects a payment for an unknown provider.

Deployments have no foreign key to transactions. That is deliberate: nothing in the data says "this deployment caused that failure". Any link between them is a timing correlation an investigator must reason about.

## `payments.providers`

| Column | Type | Meaning |
|---|---|---|
| provider_code | text, PK | Short ID: `NOVAPAY`, `KESTREL`, `BLUERIVER`, `ORBIT` |
| display_name | text, unique | Human-readable name, e.g. "Kestrel Gateway" |

## `payments.transactions` (about 490,000 rows)

| Column | Type | Meaning |
|---|---|---|
| transaction_id | text, PK | `TXN-00000001` onwards, in time order |
| created_at | timestamptz | When the payment was attempted (UTC) |
| customer_id | text | Synthetic customer, `CUST-000001` to `CUST-060000` |
| provider_code | text, FK → providers | Which provider processed it |
| payment_method | text | `CARD`, `BANK_TRANSFER`, `WALLET`, `NET_BANKING` |
| amount | numeric(12,2), > 0 | Payment value (transaction value, **not** company revenue) |
| currency | char(3) | `USD` for the whole dataset |
| status | text | `SUCCEEDED` or `FAILED` |
| failure_reason | text | Set only when `FAILED` (rule enforced by a CHECK constraint). See codes below. |
| latency_ms | integer | Processing time. Timeouts are about 30,000 ms. |
| provider_event_id | text, FK → provider_incidents, nullable | Provider notice cited in the provider's response. Only appears **after** the notice was published. |

**Failure reason codes**

| Code | Plain meaning |
|---|---|
| INSUFFICIENT_FUNDS | Customer didn't have enough money |
| DO_NOT_HONOR | Customer's bank declined without a specific reason |
| AUTHENTICATION_FAILED | Customer failed a security check (e.g. one-time code) |
| FRAUD_SUSPECTED | Blocked by fraud screening |
| INVALID_DETAILS | Wrong or expired payment details |
| TIMEOUT | Provider didn't answer in time |
| PROVIDER_ERROR | Provider returned a technical error |

The first five are mostly about the **customer**. `TIMEOUT` and `PROVIDER_ERROR` point to a **technical** problem, which is why a jump in them matters.

## `payments.provider_incidents`

| Column | Type | Meaning |
|---|---|---|
| incident_id | text, PK | `INC-0001` onwards |
| provider_code | text, FK → providers | Provider that posted the notice |
| reported_start_at / reported_end_at | timestamptz | What the provider **said**. Real problems can start earlier. |
| severity | text | `info`, `minor`, `major`, `critical` |
| title, description | text | Status-page wording (synthetic) |

## `payments.deployments`

| Column | Type | Meaning |
|---|---|---|
| deployment_id | text, PK | `DEP-0001` onwards, in time order |
| service, version | text (unique together) | What was released, e.g. `checkout-service v2.10` |
| deployed_at | timestamptz | Release time (UTC) |
| environment | text | `production` |
| team | text | Owning team (synthetic) |
| change_summary | text | Short description |

## `payments.daily_stats` (derived)

Built by the loader with one `INSERT ... SELECT` from `transactions`. One row per UTC day, provider and payment method: `attempts`, `failures`, `total_value`, `failed_value`, `avg_latency_ms`. Totals always equal the totals in `transactions`; a test checks this.

## `payments.dataset_runs`

The loaded dataset: `dataset_id`, `seed`, `window_start`, `window_end`, generation `parameters`, file fingerprints (`files`), and `library_versions`. It deliberately holds **no** incident definitions.

## `ops.investigations` (history, Phase 6)

One row per investigation. Written only by the history login; schema `ops` survives data reloads.

| Column | Type | Meaning |
|---|---|---|
| `investigation_id` | UUID, key | The id shown in the API, the dashboard and the CLI |
| `question` | text (1–2000 chars) | The question as asked (whitespace collapsed) |
| `as_of` | timestamptz, nullable | The moment treated as "now"; NULL = end of the loaded data |
| `use_llm` | boolean | Whether the local LLM was requested |
| `status` | text | `running`, `complete`, `partial` or `interrupted` |
| `mode`, `mode_reason` | text | `llm` or `template`, and why |
| `headline` | text | One-line summary |
| `dataset_id` | text | The dataset the investigation ran on (resume refuses a different one) |
| `runs` | integer ≥ 1 | 1 plus the number of resumes |
| `created_at`, `updated_at`, `finished_at` | timestamptz | `updated_at` is the heartbeat, refreshed after every step |
| `duration_ms` | integer | Duration of the latest run |
| `result` | JSONB | The full result as returned by the API (findings, evidence, trace, verification, report text) |

## `ops.investigation_steps` (checkpoints, Phase 6)

One row per step per run. Key: (`investigation_id`, `step`, `run`). Deleted with its investigation.

| Column | Type | Meaning |
|---|---|---|
| `step` | text | `plan`, `transaction`, `pattern`, `forecast`, `impact`, `assemble`, `narrate`, `verify` or `finalize` |
| `run` | integer | Which run produced it (1 = first) |
| `status` | text | `ok`, `failed` or `skipped` |
| `reusable` | boolean | Whether a resumed run may replay it |
| `payload` | JSONB | The step's state update, validated against `InvestigationState` when loaded |
| `duration_ms`, `recorded_at` | | Timing |

## Files written by the generator (`data/generated/`, not committed to Git)

| File | Loaded into the DB? | Purpose |
|---|---|---|
| providers.csv, provider_incidents.csv, deployments.csv, transactions.csv | Yes | The data |
| manifest.json | Yes (as `dataset_runs`) | Seed, parameters, row counts, SHA-256 fingerprint of each CSV |
| ground_truth.json | **Never** | Answer key for evaluation: actual incident times, injected failure counts, which deployment is the red herring |

**Why ground truth is kept separate:** if the agents could read the answer key, the evaluation would be meaningless. It's like letting a student see the exam answers.

## The scenario in the default dataset (seed 42)

| Event | When (UTC) | Notes |
|---|---|---|
| Scheduled maintenance notice, BlueRiver | Day 41 (10 Aug), 02:00–03:00 | No effect on failures (negative example) |
| Secondary incident, Orbit `PROVIDER_ERROR` | 28 Aug, 10:00–13:00 | Smaller, shorter |
| Red-herring deployment (`fraud-rules`) | 21 Sep, 10:00 | No effect |
| Correlated deployment (`checkout-service`) | 23 Sep, 12:00 | 2 hours before the primary incident; does **not** cause it |
| Primary incident, Kestrel `TIMEOUT` | 23 Sep 14:00 to 25 Sep 02:00 | Kestrel failure rate rises from about 3% to over 20% in the worst hours |
| Kestrel status notice posted | 23 Sep, 17:00 | 3 hours after the problem began |

"Last week" means the last complete Monday-to-Sunday week before the data ends: 21–27 Sep 2026.
