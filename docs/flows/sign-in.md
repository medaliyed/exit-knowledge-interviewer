# Email/password sign in

**Entry:** an operator's invitation/reset link, or **Sign in** at `/`.
**Outcome:** a bounded browser session for an existing account, with the same
subject, run ACLs and expert-name approval rules as bearer authentication.

## Configure and invite

Apply migrations with `uv run eki migrate`. Set `EKI_PUBLIC_ORIGIN` to the exact
scheme, host and port visible in the browser, with no path. The development
default is `http://localhost:8000`; production requires HTTPS. For the Vite
development proxy, use `http://127.0.0.1:5173` and restart FastAPI after changing
the origin. The origin check does not trust proxy or forwarding headers.

```bash
uv run eki users add --email nadia.ferrand@example.com --name "Nadia Ferrand" --subject nadia
uv run eki access --run RUN_ID --owner nadia
uv run eki users list
```

The `add` command prints a single-use invitation URL. Hand it to the person
through an existing channel; there is no outgoing email. Its lifetime is
72 hours. The email must belong to `EKI_WORK_EMAIL_DOMAINS` unless the operator
passes `--allow-external-domain`. Accounts have unique normalised emails and
stable subjects. `users update --email CURRENT` accepts `--new-email` and/or
`--name`; it cannot change the subject or admin flag. Domain and bearer identity
checks apply to updates too. Name changes must preserve existing approval/expert
consistency. Use the same subject, name and admin flag when migrating an
existing bearer identity; conflicting identities are refused.

The URL uses `/workspace/set-password#token=...`. The fragment is not sent to
the server. The page reads and removes it from history immediately, then sends
it only in the setup POST body. Closing or reloading that page after the
fragment is removed requires reopening the original link. A successful setup
consumes the link, revokes older sessions and signs the person in. Creating
another link invalidates any earlier unused one.

## Session flow

```mermaid
sequenceDiagram
    participant Browser
    participant API
    participant Postgres
    Browser->>API: POST /auth/login (email, password, Origin)
    API->>Postgres: Check throttle and account; verify password
    API->>Postgres: Store hashed session ID and authentication audit
    API-->>Browser: Identity + HttpOnly session cookie
    Browser->>API: GET /auth/session after reload (cookie)
    API->>Postgres: Check enabled account, expiry and last activity
    API-->>Browser: Identity
    Browser->>API: Data request (cookie, Origin for mutations)
    API->>Postgres: Recheck session, then existing run ACLs
    API-->>Browser: Permitted data only
```

The secure cookie is `__Host-eki_session`, with `HttpOnly`, `Secure`,
`SameSite=Strict`, `Path=/` and no `Domain`. Plain HTTP on non-localhost
development hosts can use `EKI_SESSION_COOKIE_SECURE=false`; this uses
`eki_session` because browsers reject a `__Host-` cookie without Secure.
Insecure cookies are refused in production.

`EKI_SESSION_IDLE_HOURS` defaults to 8 and `EKI_SESSION_MAX_DAYS` to 7. The first
limit reached ends the session. Last activity is written at most once a minute.
Cookies contain random identifiers; Postgres stores only their SHA-256 hashes.
Every request rechecks whether the account remains enabled. No account email,
password, link or session credential is written to browser persistent storage.
Unsent answers remain in tab memory even though sign-in survives reloads.

Cookie-authenticated POST, PUT, PATCH and DELETE requests require `Origin` to
match `EKI_PUBLIC_ORIGIN`; missing or foreign origins get 403 `csrf_rejected`.
Login, setup and logout apply the same check. JSON authentication request bodies
are validated without echoing submitted values. Bearer requests retain the
existing CLI/API behavior and take precedence when both credentials are present.

## Passwords and recovery

Passwords accept 12–128 printable Unicode characters, including spaces. A
local bundled 100,000-password list rejects common values; the policy also
rejects the account's name/email local part and the current password. The
standard-library scrypt hash uses a unique salt and stored parameters, with
parameter upgrades on successful sign-in. Passwords never go to a model,
trace, audit record or storage function as ordinary persisted text.

The account menu's **Change password** requires the current password. Success
keeps this browser signed in and revokes other sessions. A wrong current
password leaves this session intact. **Sign out** confirms an unsent answer,
revokes the current server session and clears the cookie.

For a forgotten password or lost device, a trusted operator can run:

```bash
uv run eki users reset-password --email nadia.ferrand@example.com
uv run eki users revoke-sessions --email nadia.ferrand@example.com
uv run eki users disable --email nadia.ferrand@example.com
uv run eki users enable --email nadia.ferrand@example.com
uv run eki audit --auth --after 0 --limit 100
```

A reset link lasts one hour. Expired, reused, superseded and unknown links all
return the same invalid-link message. Disabling an account revokes all its
sessions and takes effect on the next request. Enabling it does not restore
revoked sessions. Account changes require no service restart. Accounts are
disabled rather than deleted, retaining stable audit subjects.

## Failures and audit

Unknown emails, wrong passwords, disabled accounts and accounts with no password
all get the same 401 response. Unknown accounts still incur a dummy hash check.
After five failed attempts per email in fifteen minutes, backoff starts at one
minute and doubles with further failures, capped at fifteen minutes. Fifty
failures per client IP block it for fifteen minutes. Link endpoints share the
per-IP limit. The API returns 429 and `Retry-After`; the UI shows retry minutes.

Authentication audit is run-less and metadata only: operation, phase, account
UUID and stable subject where known. No email, password, raw link, session ID
or IP is included. Local operators page it with `eki audit --auth`; no browser
authentication-audit route is exposed. Sign-in maintenance prunes expired
sessions, used/expired links and throttle rows older than twenty-four hours.

`tests/test_accounts.py`, `tests/test_users_cli.py` and the authentication API
tests exercise these contracts against disposable database schemas. Frontend
transport and browser tests cover the sign-in/setup/account screens and session
restoration. Automated checks do not replace manual screen-reader validation or
the separate real-provider acceptance work.
