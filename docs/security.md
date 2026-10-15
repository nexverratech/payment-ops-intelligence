# Security

How the system protects data, decides who may do what, and records what happened. Everything runs on one computer, so the controls are sized for that. Where a production deployment would need more, it says so.

## Assets and threats

| Asset | What could go wrong | Main controls |
|---|---|---|
| Payment data (synthetic here) | Read by someone without a role that allows it; changed by a bug or a misbehaving agent | Sign-in and permissions on every API route; agents query through a read-only database login |
| Investigation results | Seen by another analyst; altered | Ownership checks (404 for others' investigations); history login cannot delete |
| Accounts | Password guessing; stolen session; leftover access after leaving | scrypt hashes; lockout; server-side sessions with idle and absolute timeouts; disable ends sessions at once |
| Audit log | Edited to hide an action | No UPDATE/DELETE grant; a trigger blocks changes even by the owner; hash chain detects edits |
| The LLM | Prompt injection in a question widens access or invents facts | The LLM has no tools, credentials or say in authorisation; its plan and every sentence are validated by code |
| Secrets (`.env`) | Committed to Git; leaked in logs or errors | `.env` git-ignored, `.env.example` placeholders only; passwords never logged or returned; errors reduced to types |

Out of scope for a local, single-machine portfolio project:
- Network attackers. Everything binds to 127.0.0.1, so there is no TLS.
- A compromised Windows account. It can read `.env`.
- Database superusers. They can disable triggers; the hash chain then shows the change.

## Authentication

| Control | Implementation | Source of the rule |
|---|---|---|
| Local accounts, no third-party identity provider | `ops.users`, managed with `python -m payops.security.manage` or the admin API | Project rule |
| Password hashing | scrypt, N=2¹⁷, r=8, p=1, 16-byte salt, 32-byte key, from Python's standard library; parameters stored with each hash; weaker hashes upgraded at the next sign-in | [OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html) (scrypt minimum; Argon2id is listed first but would need the `argon2-cffi` package) |
| Password policy | 12–128 characters, not the username, not trivially repetitive; no forced symbol rules | NIST SP 800-63B approach (length over composition) |
| Brute force | 5 wrong passwords in a row lock the account for 15 minutes (`LOGIN_MAX_FAILURES`, `LOGIN_LOCKOUT_MINUTES`); the right password is refused while locked; an admin can unlock | — |
| Username probing | Same message for unknown user and wrong password; unknown users still cost one hash computation, so timing matches | — |
| Sessions | 256-bit random token (`secrets.token_urlsafe(32)`), sent as `Authorization: Bearer`; only its SHA-256 is stored; idle timeout 30 min, absolute 8 h | [OWASP Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html) |
| Ending sessions | Logout, disabling an account, changing a role, or any password change or reset ends that user's sessions on their next request | — |
| Fail closed | If the application login isn't configured, every protected route answers 503; nothing runs unauthenticated | — |

## Authorisation (role-based access control)

Rules live in one file, `src/payops/security/rbac.py`, and are enforced by FastAPI dependencies in `src/payops/api/auth.py`. A test (`test_no_route_is_left_unprotected`) fails if any route other than `/health` and `/api/v1/auth/login` lacks the sign-in dependency.

| Permission | viewer | analyst | lead | admin |
|---|:-:|:-:|:-:|:-:|
| `analytics:read`: dashboard data, KPIs, anomalies | ✅ | ✅ | ✅ | ✅ |
| `investigations:run` | | ✅ | ✅ | |
| `investigations:read_own` | | ✅ | ✅ | |
| `investigations:read_all` | | | ✅ | ✅ |
| `investigations:resume_own` | | ✅ | ✅ | |
| `investigations:resume_all` | | | ✅ | |
| `users:manage` | | | | ✅ |
| `audit:read` | | | | ✅ |

**Separation of duties.** The admin manages accounts and reads the audit log but cannot run or resume investigations. A lead oversees everyone's investigations but cannot change accounts.

**Ownership.** An investigation records who started it. Asking for one you may not see answers **404**, not 403, so ids can't be probed. A refused permission answers 403 and writes an `access.denied` audit event.

**The LLM never authorises anything.** It receives no credentials and calls no tools. Its plan proposal is checked against the known provider list and the data window, and every sentence it writes is verified (docs/agent_design.md). Which endpoint runs, and for whom, is decided before the LLM is involved. Evaluation cases 7 and 8 test injection attempts against this.

**The dashboard hides tabs a role can't use.** That is only for tidiness: the API refuses those requests anyway. Each browser session has its own API client and sign-in token, and cached data is keyed by token, so one user's data is never served to another session.

## Database logins: least privilege

| Login | Rights | Used by |
|---|---|---|
| owner (`POSTGRES_USER`) | Everything | Loader, `setup_app`, health checks, cleanup in tests |
| `payops_readonly` | SELECT on `payments.*`; every session read-only; 30 s per query | All analytics and every agent tool |
| `payops_app` | SELECT/INSERT/UPDATE on `ops.investigations`, `ops.investigation_steps`, `ops.users`, `ops.sessions`; SELECT/INSERT on `ops.audit_log`; 10 s per query | History, checkpoints, sign-in, audit |

Neither application login can delete anything. `payops_app` can't see payment data, and `payops_readonly` can't see accounts or history. These grants are checked against the real database by `tests/integration/test_history_live.py` and `test_auth_live.py`.

## Audit log

**What is recorded** (`ops.audit_log`):

| Category | Events |
|---|---|
| Sign-in | `auth.login` (success, and failure with reason), `auth.logout`, `auth.password_change` |
| Investigations | `investigation.start`, `.finish`, `.view`, `.resume` |
| Accounts | `user.create`, `.update`, `.password_reset`, `.unlock` |
| Refusals and checks | `access.denied`, `audit.verify` |

Each event stores:
- the actor and their role
- the target (for example an investigation id) and the outcome
- its source: `api`, or `cli` with the Windows user name
- small structured details

Never recorded: passwords, tokens, question text, report text.

**Integrity:**
1. No UPDATE or DELETE grant for the application login.
2. Database triggers reject UPDATE, DELETE and TRUNCATE for everyone, including the owner.
3. A SHA-256 hash chain: each row's hash covers its content and the previous row's hash.

Check the chain with `python -m payops.security.manage verify-audit`, or from the dashboard's **Audit log** tab. It names the first row that was changed or whose predecessor was removed. Verified against a real PostgreSQL round trip, including after a deliberate edit (docs/evaluation.md).

**Limitation:** removing the newest rows leaves a shorter chain that still verifies. Note the latest hash that `verify-audit` prints and compare later.

**If writing the audit record fails, the action fails too** ("no audit, no action").

## Input handling

**API inputs:**
- Every input is validated by Pydantic models: lengths, patterns, ranges, timezone-aware times, UUID ids.
- Request bodies over `MAX_REQUEST_BYTES` (64 KiB) get 413.
- Questions with control characters or Unicode text-direction overrides are refused. These can hide instructions from a human reader while the model still sees them.

**SQL:** always parameterised, through SQLAlchemy `text()` with bound values. Identifiers such as role names go through `psycopg.sql.Identifier`.

**Responses:**
- Errors return generic messages: no SQL, connection strings or stack traces.
- Every response sets `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.

## Secrets

- Three database passwords live in `.env`, which is git-ignored. `.env.example` has placeholders only.
- Passwords are never put into connection-URL strings that get logged: SQLAlchemy's `URL.create` hides them when printed, and setup scripts connect with keyword arguments.
- `tests/test_health_api.py` checks the database password never appears in an API response.

## What a production deployment would add

- TLS and a reverse proxy, if it ever leaves 127.0.0.1.
- An organisation's identity provider (OIDC/SAML) instead of local accounts.
- Multi-factor authentication.
- Secrets in a vault rather than `.env`.
- Shipping audit events to a separate write-once store, which also closes the newest-rows gap.
- Per-provider data scoping for analysts. Today every role with `analytics:read` sees all providers.
- Dependency vulnerability scanning in CI (Phase 8).
