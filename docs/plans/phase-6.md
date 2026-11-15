# Phase 6 PDR: email and password sign-in

**Status:** implemented 2026-11-15, authorized by the owner's request to continue
Phase 6 backend and frontend. Verification evidence is in the Outcome section;
a manual pass in a real browser and the statistical timing check remain open.
The choices below follow the plan's existing recommendations without new dependencies.

**Parent:** [Project PDR](../PDR.md), new requirement **F19**. This supersedes
the "user accounts, passwords or single sign-on" exclusion in
[ui-pdr.md §9](../ui/ui-pdr.md#9-out-of-scope-for-this-version) and changes
[ui-pdr.md §5.1](../ui/ui-pdr.md#51-sign-in). All existing redaction,
confidentiality, approval, access and audit rules still apply.

**Goal:** people sign in to the browser workspace with their work email and a
password. They stay signed in across page reloads for a bounded time. The
access model and the CLI are unchanged.

## 1. Problem and baseline

Today every person signs in by pasting a bearer token
(`src/eki/auth.py`, `TokenAuthenticator`). Tokens are configured as JSON in
`EKI_API_TOKENS` and held only in tab memory.

| Gap in the current implementation | Consequence |
| --- | --- |
| The expert has to paste a 32+ character token they were sent by hand. | Sign-in is unfamiliar and error-prone for a non-technical, departing expert. |
| The token lives only in tab memory. | Every refresh signs the person out. Interviews happen over many sittings, so this happens often. |
| Identities live in an environment variable. | Adding a person or rotating a token means editing secret configuration and restarting the server. |
| A token cannot be revoked per session. | A token copied from a shared screen stays valid until the server is reconfigured. |
| There is no sign-in history. | The audit log can't show who signed in, when, or failed attempts. |

**Baseline that stays the same:** a `Principal` has `subject`, `name` and
`admin` fields. Run ACLs refer to `subject` (`src/eki/access.py`). Approvals
require `name` to match the run expert. Data routes are guarded by middleware
in `src/eki/api/security.py`.

## 2. Users and outcomes

| User | Outcome |
| --- | --- |
| **Expert** (Nadia) | Opens an invitation link, sets a password once, then signs in with email and password. Reloading the page keeps her signed in. |
| **Reader** (Sam) | The same sign-in. Sees only approved knowledge, as today. |
| **Operator / admin** | Creates, disables and resets accounts from the CLI without restarting the server. Can revoke a person's sessions immediately. |
| **Automation and CLI** | Keep using bearer tokens. Nothing changes. |

## 3. Scope

**In scope**

- An account store in Postgres: email, display name, subject, admin flag,
  password hash, disabled flag.
- Sign-in, sign-out and current-session endpoints.
- Server-side sessions carried by a secure cookie.
- One-time links, created by the operator, for setting the first password and
  for resetting a forgotten one.
- Signed-in users can change their own password.
- Brute-force throttling.
- Audit events for every authentication action.
- UI: a sign-in screen, a set-password screen, an account menu (change
  password, sign out) and session-expiry handling.
- CLI commands to manage accounts.
- Bearer tokens keep working alongside sessions, for the CLI and automation.

**Out of scope for this phase** (candidates for a later phase)

- Self-registration. Every account is created by an operator.
- Sending email. The system has no outgoing mail. Invitation and reset links
  are printed by the CLI and handed over by the operator through an existing
  channel.
- A "forgot password" form that emails a link. This needs outgoing mail.
- Multi-factor authentication. This is the recommended next step after this
  phase.
- SSO, OIDC or SAML.
- A browser admin screen for managing accounts.

## 4. Requirements

### 4.1 Accounts

| ID | Requirement |
| --- | --- |
| A1 | An account has: `id` (UUID), `subject` (stable, unique, the same value used in ACLs), `email` (unique, normalised), `name` (display name, used for approval matching), `admin` (bool), `password_hash` (nullable until the password is set), `disabled` (bool), and timestamps `created_at`, `password_changed_at` and `last_sign_in_at`. |
| A2 | Emails are normalised by trimming and lower-casing the whole address. Uniqueness is checked on the normalised value. |
| A3 | An account's email must belong to a domain in `EKI_WORK_EMAIL_DOMAINS`, unless the operator passes `--allow-external-domain`. This uses the same domain list that redaction trusts. |
| A4 | `subject` never changes after creation. The email and name can be changed by an operator. Changing the name re-checks the existing rule that names bound to approvals must match the run expert. |
| A5 | A disabled account cannot sign in, and all of its sessions are revoked when it is disabled. Accounts are disabled, never deleted, so audit `user_id` values stay resolvable. |

### 4.2 Passwords

| ID | Requirement |
| --- | --- |
| P1 | Passwords are 12 to 128 characters. All printable Unicode and spaces are allowed. There are no character-class rules and no forced periodic rotation (NIST SP 800-63B). |
| P2 | A password is refused if it is in a bundled list of common or breached passwords, if it contains the account's email local part or name, or if it is the same as the current password. The check is local only: passwords are never sent to an external service. |
| P3 | Passwords are hashed with a memory-hard KDF and a unique salt per password. The stored string includes the algorithm and parameters, so parameters can be raised later. Hashes are upgraded on the next successful sign-in when the parameters are outdated. See decision D1 for the algorithm. |
| P4 | Plain-text passwords are never stored, logged, traced, audited, returned in a response or sent to a model. Request bodies on `/auth/*` routes are never logged. The password field gets `RedactedText`-style typing so it cannot reach storage functions by mistake. |
| P5 | Changing a password requires the current password. It revokes all other sessions of that account and keeps the current one. |

### 4.3 Invitation and reset links

| ID | Requirement |
| --- | --- |
| L1 | `eki users add` and `eki users reset-password` create a single-use link token: 32 random bytes, with only a SHA-256 hash stored. Default lifetime is 72 hours for invitations and 1 hour for resets. |
| L2 | The token travels in the URL **fragment**: `/workspace/set-password#token=…`. Browsers don't send fragments to the server, so the token never reaches access logs or proxies. The page sends it in a POST body. |
| L3 | Using a link sets the password, marks the token used, revokes the account's existing sessions, and signs the person in. An expired, used or unknown token gets one generic message: "This link is no longer valid. Ask the person who invited you for a new one." |
| L4 | Creating a new link for an account invalidates any earlier unused link for it. |

### 4.4 Sign-in and sessions

| ID | Requirement |
| --- | --- |
| S1 | `POST /auth/login` takes `{"email","password"}`. On success it sets the session cookie and returns the identity (`subject`, `name`, `admin`, `email`). |
| S2 | Every failure returns the same 401 with "Email or password is incorrect": unknown email, wrong password, disabled account, or no password set yet. For an unknown email, the server still runs a hash check against a fixed dummy hash, so response timing doesn't reveal whether an account exists. |
| S3 | A session is a server-side row: a random 32-byte ID with only its hash stored, the account ID, `created_at`, `last_seen_at`, `expires_at`, and a user-agent summary. The cookie holds only the raw ID. |
| S4 | The cookie is named `__Host-eki_session`, with `HttpOnly; Secure; SameSite=Strict; Path=/` and no `Domain`. In `EKI_ENV=dev`, `EKI_SESSION_COOKIE_SECURE=false` uses the unprefixed `eki_session` cookie for plain HTTP because browsers reject `__Host-` without Secure. The server refuses this setting in `prod`. |
| S5 | Sessions end after an idle timeout (default 8 hours, `EKI_SESSION_IDLE_HOURS`) or an absolute lifetime (default 7 days, `EKI_SESSION_MAX_DAYS`), whichever comes first. `last_seen_at` is updated at most once a minute to limit writes. |
| S6 | A new session ID is issued on every sign-in, which prevents session fixation. `POST /auth/logout` deletes the session row and clears the cookie. |
| S7 | `GET /auth/session` returns the current identity, or 401. The browser calls it on load to restore a session after a reload. `GET /runbook/identity` keeps working for both kinds of credential. |
| S8 | Each request re-checks that the account is still enabled. Disabling an account or revoking its sessions takes effect on its next request. |

### 4.5 Request authentication and CSRF

| ID | Requirement |
| --- | --- |
| R1 | The security middleware accepts **either** `Authorization: Bearer` (existing `TokenAuthenticator`, unchanged) **or** a valid session cookie. Both produce the same `Principal`. If both are present the bearer header wins, and the cookie is ignored for that request. |
| R2 | Bearer subjects and account subjects share one namespace. At startup the server refuses to run if a bearer subject also exists as an account with a different `name` or `admin`. This is the same consistency rule `TokenAuthenticator` applies between tokens today. |
| R3 | Cookie-authenticated requests using `POST`, `PUT`, `PATCH` or `DELETE` must carry an `Origin` header that equals the server's own origin (`EKI_PUBLIC_ORIGIN`). Otherwise the request gets 403 `csrf_rejected`. Together with `SameSite=Strict` and JSON-only request bodies, this is the CSRF defence. Bearer requests are exempt, because browsers never attach them automatically. |
| R4 | `/auth/login`, `/auth/setup-password` and `/auth/logout` apply the same `Origin` check. |
| R5 | Responses carrying identity or data keep `Cache-Control: private, no-store`. `Vary` gains `Cookie`. |
| R6 | All existing ACL behaviour is unchanged: 401 when unauthenticated, 404 for inaccessible resources, owner/admin/reader rules, and the expert-name rule for approvals. |

### 4.6 Brute-force protection

| ID | Requirement |
| --- | --- |
| B1 | Failed sign-ins are counted per normalised email and per client IP in a Postgres table. Counting by email works whether or not the account exists, so the limit itself doesn't reveal existence. |
| B2 | After 5 failures for one email within 15 minutes, further attempts for that email are refused with 429 and `Retry-After`. The delay doubles with each further failure, up to 15 minutes. The account is never permanently locked, so an attacker can't lock out an expert on purpose. |
| B3 | After 50 failures from one IP within 15 minutes, that IP is refused with 429 for 15 minutes. |
| B4 | The UI shows: "Too many attempts. Try again in N minutes." The response is the same whether or not the account exists. |
| B5 | Link-token endpoints have the same per-IP limit. |

### 4.7 Audit

| ID | Requirement |
| --- | --- |
| U1 | New audit kinds: `auth.sign_in`, `auth.sign_in_failed`, `auth.sign_out`, `auth.session_expired`, `auth.password_set`, `auth.password_changed`, `auth.link_created`, `auth.account_created`, `auth.account_disabled`, `auth.account_enabled`, `auth.sessions_revoked`. |
| U2 | Events record the `subject` when an account is known. A failed sign-in for an unknown email records no identifying value. Emails, passwords, tokens, session IDs and IP addresses are never written to `audit_events`. Throttling keeps IPs only in its own short-lived table, pruned after 24 hours. |
| U3 | Audit events are not tied to a run, so they need a run-less listing. `eki audit --auth --after 0` is for local operators only. An admin API route can come in a later phase. |

### 4.8 CLI (trusted local operator)

```bash
uv run eki users add --email nadia.ferrand@example.com --name "Nadia Ferrand" --subject nadia [--admin]
                                                        # prints a one-time invitation link
uv run eki users reset-password --email nadia.ferrand@example.com   # prints a one-time reset link
uv run eki users update --email ... [--new-email ...] [--name ...] [--allow-external-domain]
uv run eki users disable --email ...                    # also revokes sessions
uv run eki users enable --email ...
uv run eki users revoke-sessions --email ...
uv run eki users list                                   # email, subject, name, admin, disabled, last sign-in
```

`eki access --owner SUBJECT --readers SUBJECT…` is unchanged. It refers to
subjects, which now exist as accounts, bearer identities or both.

## 5. UI changes

These replace [ui-pdr.md §5.1](../ui/ui-pdr.md#51-sign-in) and amend the global
behaviour in §5.8. They follow the existing tone and design tokens.

### 5.1 Sign in (`/` when signed out)

- **Content:** an **Email** field (`type=email`, `autocomplete=username`), a
  **Password** field (`type=password`, `autocomplete=current-password`) with a
  show/hide toggle, and a **Sign in** button. Help line: "Forgot your password?
  Ask the person who invited you for a reset link."
- **States:** empty; submitting (fields disabled, button reads "Signing in…");
  wrong credentials ("Email or password is incorrect."); throttled ("Too many
  attempts. Try again in N minutes."); server not configured (503, existing
  copy); network error (existing copy).
- After a failed attempt the password field is cleared and focused, and the
  email is kept.
- The existing left-hand welcome story stays. The text "The token stays in
  this tab only" is removed.

### 5.2 Set password (`/workspace/set-password#token=…`)

- **Content:** "Welcome, set your password" for an invitation, or "Choose a
  new password" for a reset. Two fields, **New password** and **Confirm
  password** (`autocomplete=new-password`). A live checklist shows "12 or more
  characters" and "Not a common password", where the second is checked by the
  server on submit. Button: **Set password and sign in**.
- **States:** invalid or expired link (generic message, no form); mismatch;
  too short; rejected as common or containing the email or name; success,
  which goes straight into the workspace.
- The page reads the token from the fragment, then immediately removes it from
  the address bar with `history.replaceState`, so it isn't kept in history or
  shown on screen.

### 5.3 Account menu

The header's "Signed in as **Nadia Ferrand**" becomes a menu with:

- **Change password:** a dialog with current, new and confirm fields. On
  success: "Password changed. Other devices have been signed out."
- **Sign out:** keeps the existing unsent-answer confirmation dialog, then
  calls `POST /auth/logout`.

### 5.4 Reload and expiry

- **On load:** `GET /auth/session`. On 200 the workspace opens at the current
  URL. On 401 the sign-in screen shows, and the route is kept so the person
  returns to the same place after signing in. This replaces the current rule
  that a refresh requires signing in again.
- **Expiry mid-session:** existing behaviour stays. Any 401 clears cached data
  and drafts, aborts requests, and shows "Your session has ended. Sign in again
  to reopen your saved work." The interview's receipt checks still prevent
  retyping or double-sending an accepted answer.
- **Unsent drafts** stay in tab memory only. A reload now keeps the person
  signed in but still loses unsent text. The existing `beforeunload` warning
  stays.

### 5.5 Client changes (`frontend/src/api.ts`, `App.tsx`)

- `ApiClient` sends `credentials: 'same-origin'` and no `Authorization` header
  in cookie mode. The token-in-memory path is kept behind a small adapter, so
  the API double and tests can use either mode.
- Browsers attach `Origin` to every same-origin `fetch` POST, including SSE
  message streams, so the client needs no extra CSRF token.
- No credential, email or session ID is written to `localStorage`,
  `sessionStorage` or IndexedDB.

## 6. API summary

| Method and path | Auth | Body | Result |
| --- | --- | --- | --- |
| `POST /auth/login` | none | `{email, password}` | 200 identity and cookie / 401 / 429 |
| `POST /auth/logout` | cookie | none | 204, cookie cleared |
| `GET /auth/session` | cookie or bearer | none | 200 identity / 401 |
| `POST /auth/password` | cookie | `{current_password, new_password}` | 204 / 400 `weak_password` / 401 |
| `POST /auth/setup-password` | link token in body | `{token, new_password}` | 200 identity and cookie / 400 `invalid_link` / 400 `weak_password` / 429 |

Error bodies follow the existing `{detail, code}` shape. Validation errors on
`/auth/*` never echo submitted values. FastAPI's default 422 echoes `input`,
so these routes use a handler that removes it.

## 7. Data model (migration `0012_accounts.sql`)

```text
accounts(id uuid pk, subject text unique not null, email text unique not null,
         name text not null, admin bool not null default false,
         password_hash text null, disabled bool not null default false,
         created_at, password_changed_at, last_sign_in_at)
auth_sessions(id_hash bytea pk, account_id fk accounts, created_at,
              last_seen_at, expires_at, user_agent text)        -- index on account_id, expires_at
auth_links(token_hash bytea pk, account_id fk, purpose text check in ('invite','reset'),
           created_at, expires_at, used_at null)
auth_throttle(key text, kind text check in ('email','ip'), failed_at timestamptz)  -- pruned after 24h
```

Expired sessions, used links and old throttle rows are deleted on a schedule
that runs on sign-in, so no background worker is needed. The migration extends the audit kind constraint; it does not
change existing run data or ACLs. `EKI_API_TOKENS` keeps working unchanged.

## 8. Rollout and migration

1. Apply migration 0012 (`uv run eki migrate`).
2. For each existing human bearer identity, the operator runs
   `eki users add` with the **same subject and name**, so ACLs and approval
   name matching carry over with no data change.
3. Hand each person their invitation link.
4. Once everyone has signed in, remove human tokens from `EKI_API_TOKENS`.
   Keep tokens for automation and the CLI's `EKI_API_TOKEN`.
5. Set `EKI_PUBLIC_ORIGIN` to the exact browser origin (for example
   `https://eki.example.com`); the development default is `http://localhost:8000`.
   Production requires HTTPS. An absent origin disables cookie sign-in; bearer
   access remains available.

The rollout is reversible. Removing accounts or leaving the cookie routes
unused leaves bearer access exactly as it is today.

## 9. Security considerations

| Threat | Mitigation |
| --- | --- |
| Credential stuffing or guessing | B1–B3 throttling, P2 common-password check, generic errors. MFA is the next phase. |
| Account enumeration | S2 generic errors and dummy-hash timing, B1 counting by email for every attempt, L3 generic link errors. |
| Session theft through XSS | `HttpOnly` cookie. The existing strict CSP and text-only rendering stay. |
| CSRF | `SameSite=Strict` plus the `Origin` check (R3) plus JSON-only bodies. |
| Session fixation | A new ID on every sign-in and password set (S6, L3). |
| Database leak | Only hashes of passwords, session IDs and link tokens are stored. |
| Link leakage through logs or Referer | Token in the URL fragment (L2), single use, short lifetime, invalidated by a newer link. |
| Lost device | `eki users revoke-sessions`. A password change revokes other sessions. |
| Secrets in logs, traces or audit | P4 and U2. The access log already keeps only paths. Redaction runs on any free text. |

## 10. Acceptance criteria

1. An operator creates an account and hands over the link. The expert sets a
   password and lands in the workspace without any other help.
2. After a page reload mid-interview, the expert is still signed in and on the
   same question. The pending-receipt behaviour of F14 is unchanged.
3. Wrong password, unknown email, disabled account and an account with no
   password yet all return byte-identical 401 bodies. Response times for
   unknown and known emails are within the same distribution (statistical
   test, 200 samples each).
4. The sixth failed attempt for one email within 15 minutes returns 429. A
   correct password is refused during the backoff period and accepted after
   it.
5. A cookie-authenticated `POST` with a foreign or missing `Origin` returns
   403 and has no effect.
6. Disabling an account makes its next request return 401 in an open browser
   tab.
7. A changed password signs out a second browser and keeps the current one.
8. An invitation link works once. A second use, an expired link and a link
   superseded by a newer one all show the generic invalid-link message.
9. Searching logs, traces, `audit_events`, the database and API responses for
   a test password, session ID and link token after a full run finds nothing.
   Hashes are acceptable.
10. Bearer-token CLI and API flows pass the existing test suite unchanged.
11. Reader and owner ACL tests pass for cookie-authenticated identities, and
    the approval expert-name rule holds.
12. The new screens pass the existing Playwright accessibility checks (WCAG A
    and AA at 360, 768 and 1440 px). Password managers recognise the fields:
    `autocomplete` attributes are present and the forms have proper labels.

## 11. Test plan

- **Unit:** normalisation, the password policy, hash and verify with
  parameter upgrade, cookie attributes, the `Origin` check, the backoff
  calculation, and generic error bodies.
- **Database (`-m db`):** session lifecycle, expiry and idle, revocation on
  disable or password change, single-use links, throttle counting and
  pruning, and the subject-conflict check at startup.
- **API:** all of §10 that doesn't need a browser, plus the existing
  `test_api_access.py` matrix run a second time with cookie identities.
- **Red team (`evals/run.py`):** enumeration timing, CSRF from a foreign
  origin, link reuse, secret leakage into logs and audit, and fixation.
- **Browser (`frontend/e2e`):** sign-in states, reload persistence, expiry
  mid-interview, change password, set password from a fragment link, and
  sign-out with an unsent answer.

## 12. Implementation choices

| # | Question | Options considered | Selected |
| --- | --- | --- | --- |
| D1 | Password hashing algorithm | (a) `hashlib.scrypt` from the standard library, no new dependency; (b) Argon2id through `argon2-cffi`, a **new dependency** that needs approval under the project rules | (a) scrypt (`n=2**15, r=8, p=1`), using the standard library and no new dependency. |
| D2 | Session lifetime | Idle 8 h and absolute 7 days / shorter / longer | 8 h idle and 7 days absolute: covers a working day and a week of sittings. Both are configurable. |
| D3 | Restrict account emails to work domains (A3)? | Enforce / warn only | Enforce, with an explicit operator override. |
| D4 | Keep bearer tokens for people after rollout? | Keep both / accounts only for people | Keep both during rollout, then use accounts for people and tokens for automation only. |
| D5 | Bind approval to the subject instead of the display name? | Keep the name match / move the run expert to a subject | Keep the name match in this phase, so behaviour doesn't change. Revisit together with MFA. |
| D6 | Common-password list | Bundle the top 100k list in the repo / smaller list | Bundle the top 100k list (about 1 MB, synthetic-safe public data) as a package data file. |

## 13. Deliverables and order

| Order | Deliverable | Evidence |
| --- | --- | --- |
| 6a | Migration, account store, password hashing and policy, `eki users` CLI | Unit and database tests |
| 6b | Sessions, `/auth/*` routes, middleware supporting both credentials, CSRF, throttling, audit | API, database and red-team tests |
| 6c | Frontend: sign in, set password, account menu, reload restore, expiry | Playwright and accessibility checks; a manual pass in a real browser |
| 6d | Documentation: `ui-pdr.md` §5.1 and §9, `CLAUDE.md` auth section, `docs/flows/browser-workspace.md`, a new `docs/flows/sign-in.md`, the tutorial, and F19 added to `docs/PDR.md` | Review |

Each deliverable is one commit with `F19` in the message, after pytest, ruff
and mypy pass.

## 14. Outcome (2026-11-15)

Implemented as four commits (6a to 6d) on the recommended choices: scrypt from
the standard library, 8 h idle and 7 day sessions, work-domain enforcement with
an operator override, bearer tokens kept for the CLI and automation.

Verification at the final state of the branch:

| Check | Result |
| --- | --- |
| `uv run pytest -m "not infra and not live"` (unit and database) | 702 passed |
| `uv run ruff check .` and `ruff format --check .` | clean |
| `uv run mypy` | no issues in 165 files |
| `npm run typecheck`, `npm test` | clean, 11 passed |
| `npm run test:e2e` (Chrome, mocked API, WCAG A/AA at 360/768/1440 px) | 31 passed |
| `npm run build` | reproduces the committed `src/eki/web/static/workspace/` assets |

Acceptance criteria: 1 (`tests/test_users_cli.py`), 3 to 8 and 11
(`tests/test_auth_sessions.py::TestCookieHTTP`), 9 (secret-redaction checks in
`tests/test_accounts.py` and `tests/test_auth_sessions.py`), 10 (the existing
suite passes unchanged apart from the migration count), 12
(`frontend/e2e/auth.spec.ts`) and the unit half of 2 are covered by automated
tests.

Still open:

- The statistical response-time comparison in criterion 3 (200 samples per
  case) has not been run. The mechanism, a dummy scrypt verification for unknown
  emails, is implemented and unit-tested.
- A manual pass in a real browser against the FastAPI-served build, including a
  screen reader, is pending, as it is for the rest of the workspace.
- The bearer/account subject consistency check runs at startup when the
  database is reachable, and on every sign-in. A server that starts without a
  database logs a warning instead of refusing, so a dead database at boot does
  not take the API down.
- Session expiry is not written to the audit log; expired rows are removed on
  the next request or sign-in prune. Sign-in, sign-out, password and account
  changes are audited.
