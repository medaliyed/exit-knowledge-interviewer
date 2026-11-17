"""Email/password accounts, server-side sessions, one-time links and sign-in throttling (F19).

Only hashes are stored: of passwords (scrypt, salted), of session IDs and of link
tokens. Every function that changes authentication state writes an ``auth`` audit
event whose ``user_id`` is the account subject, or ``anonymous`` when no account is
known. Emails, passwords, tokens, session IDs and client addresses never reach the
audit table.
"""

import base64
import hashlib
import hmac
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from importlib import resources
from typing import Literal
from uuid import UUID, uuid4

import psycopg
from psycopg import errors
from pydantic import SecretStr

from eki.audit import AuditDetails, Operation, Phase, append_event, audit_context
from eki.auth import Principal
from eki.config import ConfigError
from eki.errors import NotFoundError
from eki.runbook.review import check_expert

MIN_PASSWORD = 12
MAX_PASSWORD = 128
SCRYPT_N, SCRYPT_R, SCRYPT_P = 32768, 8, 1
LINK_LIFETIME = {"invite": timedelta(hours=72), "reset": timedelta(hours=1)}
EMAIL_WINDOW, EMAIL_FAILURES, EMAIL_MAX_DELAY = timedelta(minutes=15), 5, timedelta(minutes=15)
IP_WINDOW, IP_FAILURES, IP_DELAY = timedelta(minutes=15), 50, timedelta(minutes=15)
TOUCH_INTERVAL = timedelta(minutes=1)
THROTTLE_RETENTION = timedelta(hours=24)

Purpose = Literal["invite", "reset"]
PolicyCode = Literal[
    "too_short", "too_long", "common", "contains_identity", "unchanged", "not_printable"
]
type Password = SecretStr
"""Passwords cross the HTTP boundary as opaque, non-string secrets, never redacted prose."""


def _plaintext(password: str | Password) -> str:
    # Strings remain accepted for the trusted local store API and existing callers.
    return password.get_secret_value() if isinstance(password, SecretStr) else password


class PasswordPolicyError(ValueError):
    """The password does not meet the policy. The message never contains the password."""

    def __init__(self, code: PolicyCode) -> None:
        super().__init__(f"password refused: {code}")
        self.code: PolicyCode = code


class AccountExistsError(ValueError):
    """The email or subject is already taken."""


class LinkError(ValueError):
    """The link is unknown, used, expired or superseded. One message for all cases."""


class SignInRefused(Exception):
    """Wrong email or password, disabled account, or no password yet. One message for all."""


class Throttled(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("too many attempts")
        self.retry_after = retry_after


@dataclass(frozen=True)
class SessionPolicy:
    idle: timedelta
    max_age: timedelta


@dataclass(frozen=True)
class Account:
    id: UUID
    subject: str
    email: str
    name: str
    admin: bool
    disabled: bool
    has_password: bool
    created_at: datetime
    password_changed_at: datetime | None
    last_sign_in_at: datetime | None

    @property
    def principal(self) -> Principal:
        return Principal(self.subject, self.name, self.admin)


@dataclass(frozen=True)
class Signed:
    principal: Principal
    session_id: str = field(repr=False)
    """The raw session ID for the cookie. It is never stored; only its hash is."""


# --- email and passwords -----------------------------------------------------------------


def normalise_email(email: str) -> str:
    value = email.strip().lower()
    local, at, domain = value.partition("@")
    if (
        not at
        or not local
        or "." not in domain
        or domain.startswith(".")
        or domain.endswith(".")
        or "@" in domain
        or any(c.isspace() for c in value)
        or len(value) > 254
    ):
        raise ValueError("invalid email address")
    return value


@cache
def _common_passwords() -> frozenset[str]:
    text = resources.files("eki").joinpath("data/common_passwords.txt").read_text("utf-8")
    return frozenset(line.strip().lower() for line in text.splitlines() if line.strip())


def check_password(
    password: str | Password, *, email: str, name: str, current_hash: str | None = None
) -> None:
    password = _plaintext(password)
    if len(password) < MIN_PASSWORD:
        raise PasswordPolicyError("too_short")
    if len(password) > MAX_PASSWORD:
        raise PasswordPolicyError("too_long")
    if not password.isprintable():
        raise PasswordPolicyError("not_printable")
    lowered = password.lower()
    if lowered.strip() in _common_passwords():
        raise PasswordPolicyError("common")
    local = email.lower().partition("@")[0]
    identity_parts = {local, name.lower(), *name.lower().replace(".", " ").split()}
    if any(len(part) >= 3 and part in lowered for part in identity_parts):
        raise PasswordPolicyError("contains_identity")
    if current_hash is not None and verify_password(password, current_hash):
        raise PasswordPolicyError("unchanged")


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=256 * 1024 * 1024
    )


def hash_password(password: str | Password) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(_plaintext(password), salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    encode = base64.b64encode
    return (
        f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${encode(salt).decode()}${encode(digest).decode()}"
    )


def _parse(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return None
        parameters = int(n), int(r), int(p)
        if not (
            1 < parameters[0] <= 2**20 and 1 <= parameters[1] <= 32 and 1 <= parameters[2] <= 4
        ):
            return None
        salt_bytes, digest_bytes = (
            base64.b64decode(salt, validate=True),
            base64.b64decode(digest, validate=True),
        )
        if not 8 <= len(salt_bytes) <= 64 or len(digest_bytes) != 32:
            return None
        return *parameters, salt_bytes, digest_bytes
    except (ValueError, TypeError):
        return None


def verify_password(password: str | Password, stored: str) -> bool:
    parsed = _parse(stored)
    if parsed is None:
        return False
    n, r, p, salt, digest = parsed
    try:
        return hmac.compare_digest(_scrypt(_plaintext(password), salt, n, r, p), digest)
    except ValueError:
        return False


def needs_rehash(stored: str) -> bool:
    parsed = _parse(stored)
    return parsed is None or parsed[:3] != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


@cache
def _dummy_hash() -> str:
    """Verified when no account matches, so unknown emails take as long as wrong passwords."""
    # A fixed, non-credential hash avoids an extra KDF on the first unknown-email attempt.
    return "scrypt$32768$8$1$LYjbDe4gVn6Uc+xi6nwUHw==$vpmQ4FR+StSBplnfmVMBUHo161K8sYjmV8K0Q4vfPAM="


# --- storage helpers ----------------------------------------------------------------------

_COLUMNS = (
    "id, subject, email, name, admin, disabled, password_hash IS NOT NULL, created_at, "
    "password_changed_at, last_sign_in_at"
)
_JOIN_COLUMNS = (
    "a.id, a.subject, a.email, a.name, a.admin, a.disabled, a.password_hash IS NOT NULL, "
    "a.created_at, a.password_changed_at, a.last_sign_in_at"
)


def _account(row: tuple[object, ...]) -> Account:
    return Account(
        UUID(str(row[0])),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        bool(row[4]),
        bool(row[5]),
        bool(row[6]),
        _aware(row[7]),
        None if row[8] is None else _aware(row[8]),
        None if row[9] is None else _aware(row[9]),
    )


def _aware(value: object) -> datetime:
    assert isinstance(value, datetime)  # noqa: S101 - column type
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _audit(
    conn: psycopg.Connection,
    operation: Operation,
    phase: Phase,
    *,
    subject: str | None,
    account_id: UUID | None = None,
) -> None:
    with audit_context(user_id=subject or "anonymous", run_ids=()):
        append_event(conn, "auth", phase, AuditDetails(operation=operation, target_id=account_id))


def _hash(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _by_email(conn: psycopg.Connection, email: str) -> Account | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM accounts WHERE email = %s",  # noqa: S608 - fixed columns
        (email,),
    ).fetchone()
    return None if row is None else _account(row)


def _by_id(conn: psycopg.Connection, account_id: UUID, *, lock: bool = False) -> Account:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM accounts WHERE id = %s" + (" FOR UPDATE" if lock else ""),  # noqa: S608
        (account_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("account not found")
    return _account(row)


# --- accounts -----------------------------------------------------------------------------


def create_account(
    conn: psycopg.Connection,
    *,
    email: str,
    name: str,
    subject: str,
    work_domains: frozenset[str],
    admin: bool = False,
    allow_external_domain: bool = False,
    bearer_identities: list[Principal] | None = None,
    now: datetime | None = None,
) -> Account:
    address = normalise_email(email)
    name, subject = name.strip(), subject.strip()
    if not name or not subject or len(subject) > 200:
        raise ValueError("name and subject are required")
    if not allow_external_domain and address.partition("@")[2] not in work_domains:
        raise ValueError("the email is not on a configured work domain")
    for identity in bearer_identities or ():
        if identity.subject == subject and identity != Principal(subject, name, admin):
            raise ConfigError(
                "the subject is already a configured bearer identity with a different name "
                "or admin flag"
            )
    account_id = uuid4()
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO accounts (id, subject, email, name, admin, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (account_id, subject, address, name, admin, now or datetime.now(UTC)),
            )
            _audit(conn, "account_created", "completed", subject=subject, account_id=account_id)
    except errors.UniqueViolation:
        raise AccountExistsError("an account with that email or subject exists") from None
    return _by_id(conn, account_id)


def get_account(conn: psycopg.Connection, email: str) -> Account:
    account = _by_email(conn, normalise_email(email))
    if account is None:
        raise NotFoundError("account not found")
    return account


def get_account_by_subject(conn: psycopg.Connection, subject: str) -> Account:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM accounts WHERE subject = %s",  # noqa: S608
        (subject,),
    ).fetchone()
    if row is None:
        raise NotFoundError("account not found")
    return _account(row)


def validate_bearer_identities(conn: psycopg.Connection, identities: Sequence[Principal]) -> None:
    """Refuse contradictory identities in the shared bearer/account subject namespace."""
    known = {identity.subject: identity for identity in identities}
    for account in list_accounts(conn):
        if account.subject in known and account.principal != known[account.subject]:
            raise ConfigError("account and bearer identities disagree for a shared subject")


def list_accounts(conn: psycopg.Connection) -> list[Account]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM accounts ORDER BY created_at, subject"  # noqa: S608
    ).fetchall()
    return [_account(row) for row in rows]


def update_account(
    conn: psycopg.Connection,
    email: str,
    *,
    new_email: str | None = None,
    name: str | None = None,
    work_domains: frozenset[str],
    allow_external_domain: bool = False,
    bearer_identities: Sequence[Principal] = (),
) -> Account:
    """Update contact/display fields without changing the subject or prior approval identity."""
    account = get_account(conn, email)
    address = normalise_email(new_email) if new_email is not None else account.email
    display_name = name.strip() if name is not None else account.name
    if not display_name:
        raise ValueError("name is required")
    if (
        new_email is not None
        and not allow_external_domain
        and address.partition("@")[2] not in work_domains
    ):
        raise ValueError("the email is not on a configured work domain")
    try:
        with conn.transaction():
            account = _by_id(conn, account.id, lock=True)
            address = account.email if new_email is None else address
            display_name = account.name if name is None else display_name
            candidate = Principal(account.subject, display_name, account.admin)
            if any(
                identity.subject == account.subject and identity != candidate
                for identity in bearer_identities
            ):
                raise ConfigError("account and bearer identities disagree for a shared subject")
            if display_name != account.name:
                bound = conn.execute(
                    "SELECT DISTINCT r.expert_name FROM ingest_runs r WHERE "
                    "(r.owner_subject = %s AND EXISTS (SELECT 1 FROM interview_plans p "
                    "WHERE p.run_id = r.id AND p.status = 'approved')) "
                    "OR EXISTS (SELECT 1 FROM audit_events ae WHERE ae.run_id = r.id "
                    "AND ae.user_id = %s AND ae.phase = 'completed' "
                    "AND ((ae.kind = 'approval' AND ae.details->>'operation' = 'approve_plan') "
                    "OR (ae.kind = 'review' AND ae.details->>'operation' = 'approve')))",
                    (account.subject, account.subject),
                ).fetchall()
                for (expert,) in bound:
                    check_expert(display_name, expert)
            conn.execute(
                "UPDATE accounts SET email = %s, name = %s WHERE id = %s",
                (address, display_name, account.id),
            )
            _audit(
                conn, "account_updated", "completed", subject=account.subject, account_id=account.id
            )
    except errors.UniqueViolation:
        raise AccountExistsError("an account with that email or subject exists") from None
    return _by_id(conn, account.id)


def set_disabled(conn: psycopg.Connection, email: str, disabled: bool) -> Account:
    account = get_account(conn, email)
    with conn.transaction():
        account = _by_id(conn, account.id, lock=True)
        conn.execute("UPDATE accounts SET disabled = %s WHERE id = %s", (disabled, account.id))
        if disabled:
            revoke_sessions(conn, account.id)
        _audit(
            conn,
            "account_disabled" if disabled else "account_enabled",
            "completed",
            subject=account.subject,
            account_id=account.id,
        )
    return _by_id(conn, account.id)


def set_password(
    conn: psycopg.Connection, account_id: UUID, password: str | Password, *, now: datetime
) -> None:
    """Set a password directly (links and tests). Enforces the policy; revokes nothing."""
    with conn.transaction():
        account = _by_id(conn, account_id, lock=True)
        check_password(password, email=account.email, name=account.name)
        conn.execute(
            "UPDATE accounts SET password_hash = %s, password_changed_at = %s WHERE id = %s",
            (hash_password(password), now, account_id),
        )
        _audit(conn, "password_set", "completed", subject=account.subject, account_id=account_id)


# --- links --------------------------------------------------------------------------------


def create_link(
    conn: psycopg.Connection, account_id: UUID, purpose: Purpose, *, now: datetime
) -> str:
    """A single-use token. Any earlier unused link of the account stops working."""
    token = secrets.token_urlsafe(32)
    with conn.transaction():
        account = _by_id(conn, account_id, lock=True)
        conn.execute(
            "DELETE FROM auth_links WHERE account_id = %s AND used_at IS NULL", (account_id,)
        )
        conn.execute(
            "INSERT INTO auth_links (token_hash, account_id, purpose, created_at, expires_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (_hash(token), account_id, purpose, now, now + LINK_LIFETIME[purpose]),
        )
        _audit(conn, "link_created", "completed", subject=account.subject, account_id=account_id)
    return token


def redeem_link(
    conn: psycopg.Connection,
    token: str,
    password: str | Password,
    *,
    now: datetime,
    policy: SessionPolicy,
    client: str | None = None,
    user_agent: str = "",
) -> Signed:
    refusal: LinkError | Throttled | None = None
    signed: Signed | None = None
    with conn.transaction():
        if client is not None:
            _lock_throttle(conn, (("ip", client[:200]),))
            wait = _ip_retry_after(conn, client[:200], now=now)
            if wait:
                refusal = Throttled(wait)
        if refusal is None:
            # Lock the account before re-reading the token, matching create_link's lock order.
            row = conn.execute(
                "SELECT account_id FROM auth_links WHERE token_hash = %s", (_hash(token),)
            ).fetchone()
            account = None if row is None else _by_id(conn, UUID(str(row[0])), lock=True)
            link = conn.execute(
                "SELECT expires_at, used_at FROM auth_links WHERE token_hash = %s FOR UPDATE",
                (_hash(token),),
            ).fetchone()
            if (
                account is None
                or account.disabled
                or link is None
                or link[1] is not None
                or _aware(link[0]) <= now
            ):
                if client is not None:
                    conn.execute(
                        "INSERT INTO auth_throttle (key, kind, failed_at) VALUES (%s,'ip',%s)",
                        (client[:200], now),
                    )
                _audit(conn, "password_set", "failed", subject=None)
                refusal = LinkError("this link is no longer valid")
            else:
                stored = conn.execute(
                    "SELECT password_hash FROM accounts WHERE id = %s", (account.id,)
                ).fetchone()
                check_password(
                    password,
                    email=account.email,
                    name=account.name,
                    current_hash=stored[0] if stored else None,
                )
                conn.execute(
                    "UPDATE auth_links SET used_at = %s WHERE token_hash = %s", (now, _hash(token))
                )
                conn.execute(
                    "UPDATE accounts SET password_hash = %s, password_changed_at = %s "
                    "WHERE id = %s",
                    (hash_password(password), now, account.id),
                )
                revoke_sessions(conn, account.id)
                _audit(
                    conn,
                    "password_set",
                    "completed",
                    subject=account.subject,
                    account_id=account.id,
                )
                signed = _open_session(conn, account, now=now, policy=policy, user_agent=user_agent)
    if refusal is not None:
        raise refusal
    assert signed is not None  # noqa: S101 - either a refusal or a session is produced
    return signed


# --- sessions -----------------------------------------------------------------------------


def _open_session(
    conn: psycopg.Connection,
    account: Account,
    *,
    now: datetime,
    policy: SessionPolicy,
    user_agent: str,
) -> Signed:
    raw = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO auth_sessions (id_hash, account_id, created_at, last_seen_at, expires_at, "
        "user_agent) VALUES (%s,%s,%s,%s,%s,%s)",
        (_hash(raw), account.id, now, now, now + policy.max_age, _user_agent_summary(user_agent)),
    )
    conn.execute("UPDATE accounts SET last_sign_in_at = %s WHERE id = %s", (now, account.id))
    _audit(conn, "sign_in", "completed", subject=account.subject, account_id=account.id)
    return Signed(account.principal, raw)


def _user_agent_summary(user_agent: str) -> str:
    """Keep only a coarse product name, never arbitrary client-supplied header contents."""
    for marker, summary in (
        ("Firefox/", "Firefox"),
        ("Edg/", "Edge"),
        ("Chrome/", "Chrome"),
        ("Safari/", "Safari"),
    ):
        if marker in user_agent:
            return summary
    return "Other" if user_agent else ""


def _retry_after(
    conn: psycopg.Connection,
    kind: str,
    key: str,
    *,
    now: datetime,
    window: timedelta,
    threshold: int,
    base: timedelta,
    cap: timedelta,
) -> int:
    row = conn.execute(
        "SELECT count(*), max(failed_at) FROM auth_throttle "
        "WHERE kind = %s AND key = %s AND failed_at > %s",
        (kind, key, now - window),
    ).fetchone()
    count = int(row[0]) if row else 0
    if count < threshold or row is None or row[1] is None:
        return 0
    # The exponent is bounded even if a previously unthrottled store contains many rows.
    delay = min(cap, base * 2 ** min(count - threshold, 10))
    remaining = (_aware(row[1]) + delay - now).total_seconds()
    return max(0, int(remaining + 0.999))


def _throttle_keys(email: str, client: str) -> tuple[str, str]:
    return hashlib.sha256(email.encode()).hexdigest(), client[:200]


def _lock_throttle(conn: psycopg.Connection, keys: Sequence[tuple[str, str]]) -> None:
    """Serialize each counter check/update, including concurrent requests for unknown emails."""
    for kind, key in sorted(keys):
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || %s, 0))",
            (f".auth_throttle.{kind}.{key}",),
        )


def _ip_retry_after(conn: psycopg.Connection, key: str, *, now: datetime) -> int:
    return _retry_after(
        conn,
        "ip",
        key,
        now=now,
        window=IP_WINDOW,
        threshold=IP_FAILURES,
        base=IP_DELAY,
        cap=IP_DELAY,
    )


def _email_retry_after(conn: psycopg.Connection, key: str, *, now: datetime) -> int:
    return _retry_after(
        conn,
        "email",
        key,
        now=now,
        window=EMAIL_WINDOW,
        threshold=EMAIL_FAILURES,
        base=timedelta(minutes=1),
        cap=EMAIL_MAX_DELAY,
    )


def _check_throttle(
    conn: psycopg.Connection, email_key: str, ip_key: str, *, now: datetime
) -> None:
    wait = max(
        _email_retry_after(conn, email_key, now=now),
        _ip_retry_after(conn, ip_key, now=now),
    )
    if wait:
        raise Throttled(wait)


def _record_failure(
    conn: psycopg.Connection, email_key: str, ip_key: str, *, now: datetime
) -> None:
    conn.execute(
        "INSERT INTO auth_throttle (key, kind, failed_at) VALUES (%s,'email',%s), (%s,'ip',%s)",
        (email_key, now, ip_key, now),
    )


def sign_in(
    conn: psycopg.Connection,
    email: str,
    password: str | Password,
    *,
    now: datetime,
    client: str,
    policy: SessionPolicy,
    user_agent: str = "",
) -> Signed:
    try:
        address = normalise_email(email)
    except ValueError:
        address = email.strip().lower()
    email_key, ip_key = _throttle_keys(address, client)
    refusal: SignInRefused | Throttled | None = None
    signed: Signed | None = None
    # Housekeeping has its own transaction: never hold counter locks while pruning other accounts.
    with conn.transaction():
        prune(conn, now=now, policy=policy)
    with conn.transaction():
        _lock_throttle(conn, (("email", email_key), ("ip", ip_key)))
        account = _by_email(conn, address)
        try:
            _check_throttle(conn, email_key, ip_key, now=now)
        except Throttled as error:
            _audit(conn, "sign_in", "refused", subject=account.subject if account else None)
            refusal = error
        if refusal is None:
            if account is not None:
                account = _by_id(conn, account.id, lock=True)
            stored = (
                conn.execute(
                    "SELECT password_hash FROM accounts WHERE id = %s", (account.id,)
                ).fetchone()
                if account
                else None
            )
            current = stored[0] if stored and stored[0] else None
            valid = verify_password(password, current or _dummy_hash()) and current is not None
            if account is None or not valid or account.disabled:
                _record_failure(conn, email_key, ip_key, now=now)
                _audit(
                    conn,
                    "sign_in",
                    "refused" if account and account.disabled else "failed",
                    subject=account.subject if account else None,
                    account_id=account.id if account else None,
                )
                refusal = SignInRefused("email or password is incorrect")
            else:
                if current is not None and needs_rehash(current):
                    conn.execute(
                        "UPDATE accounts SET password_hash = %s WHERE id = %s",
                        (hash_password(password), account.id),
                    )
                signed = _open_session(conn, account, now=now, policy=policy, user_agent=user_agent)
    # Failed-attempt counters and audit must commit before the expected exception escapes.
    if refusal is not None:
        raise refusal
    assert signed is not None  # noqa: S101 - either a refusal or a session is produced
    return signed


def authenticate_session(
    conn: psycopg.Connection, session_id: str, *, now: datetime, policy: SessionPolicy
) -> Principal | None:
    """The principal of a live session, or ``None``. Expired sessions are removed."""
    key = _hash(session_id)
    row = conn.execute(
        f"SELECT s.last_seen_at, s.expires_at, {_JOIN_COLUMNS} "  # noqa: S608
        "FROM auth_sessions s JOIN accounts a ON a.id = s.account_id WHERE s.id_hash = %s",
        (key,),
    ).fetchone()
    if row is None:
        return None
    last_seen, expires_at, account = _aware(row[0]), _aware(row[1]), _account(row[2:])
    if now >= expires_at or now - last_seen >= policy.idle or account.disabled:
        with conn.transaction():
            deleted = conn.execute("DELETE FROM auth_sessions WHERE id_hash = %s", (key,)).rowcount
            if deleted:
                _audit(
                    conn,
                    "session_expired",
                    "completed",
                    subject=account.subject,
                    account_id=account.id,
                )
        return None
    if now - last_seen >= TOUCH_INTERVAL:
        conn.execute("UPDATE auth_sessions SET last_seen_at = %s WHERE id_hash = %s", (now, key))
    return account.principal


def sign_out(conn: psycopg.Connection, session_id: str) -> None:
    with conn.transaction():
        row = conn.execute(
            "DELETE FROM auth_sessions WHERE id_hash = %s RETURNING account_id",
            (_hash(session_id),),
        ).fetchone()
        if row is not None:
            account = _by_id(conn, UUID(str(row[0])))
            _audit(conn, "sign_out", "completed", subject=account.subject, account_id=account.id)


def revoke_sessions(conn: psycopg.Connection, account_id: UUID, *, keep: str | None = None) -> int:
    with conn.transaction():
        account = _by_id(conn, account_id, lock=True)
        deleted = conn.execute(
            "DELETE FROM auth_sessions WHERE account_id = %s AND id_hash IS DISTINCT FROM %s",
            (account_id, None if keep is None else _hash(keep)),
        ).rowcount
        _audit(
            conn, "sessions_revoked", "completed", subject=account.subject, account_id=account_id
        )
    return deleted


def change_password(
    conn: psycopg.Connection,
    session_id: str,
    current: str | Password,
    new: str | Password,
    *,
    now: datetime,
    policy: SessionPolicy,
) -> None:
    refusal: SignInRefused | Throttled | None = None
    with conn.transaction():
        row = conn.execute(
            "SELECT s.account_id, a.email FROM auth_sessions s JOIN accounts a "
            "ON a.id = s.account_id WHERE s.id_hash = %s",
            (_hash(session_id),),
        ).fetchone()
        # Wrong current passwords share the per-account sign-in counter, so a stolen
        # session cannot guess it without limit. Counter before account row, as in sign_in.
        email_key = None if row is None else _throttle_keys(str(row[1]), "")[0]
        if email_key is not None:
            _lock_throttle(conn, (("email", email_key),))
        account = None if row is None else _by_id(conn, UUID(str(row[0])), lock=True)
        principal = authenticate_session(conn, session_id, now=now, policy=policy)
        if principal is None or account is None or email_key is None:
            refusal = SignInRefused("sign in again")
        elif wait := _email_retry_after(conn, email_key, now=now):
            _audit(conn, "password_changed", "refused", subject=principal.subject)
            refusal = Throttled(wait)
        else:
            stored = conn.execute(
                "SELECT password_hash FROM accounts WHERE id = %s", (account.id,)
            ).fetchone()
            current_hash = stored[0] if stored else None
            if not current_hash or not verify_password(current, current_hash):
                conn.execute(
                    "INSERT INTO auth_throttle (key, kind, failed_at) VALUES (%s,'email',%s)",
                    (email_key, now),
                )
                _audit(conn, "password_changed", "failed", subject=principal.subject)
                refusal = SignInRefused("the current password is incorrect")
            else:
                check_password(
                    new, email=account.email, name=account.name, current_hash=current_hash
                )
                conn.execute(
                    "UPDATE accounts SET password_hash = %s, password_changed_at = %s "
                    "WHERE id = %s",
                    (hash_password(new), now, account.id),
                )
                revoke_sessions(conn, account.id, keep=session_id)
                _audit(
                    conn,
                    "password_changed",
                    "completed",
                    subject=account.subject,
                    account_id=account.id,
                )
    if refusal is not None:
        raise refusal


def prune(conn: psycopg.Connection, *, now: datetime, policy: SessionPolicy | None = None) -> None:
    """Drop expired sessions, used or expired links and old throttle rows. Runs on sign-in."""
    idle_before = now - policy.idle if policy is not None else datetime.min.replace(tzinfo=UTC)
    expired = conn.execute(
        "SELECT a.id, a.subject FROM accounts a WHERE EXISTS "
        "(SELECT 1 FROM auth_sessions s WHERE s.account_id = a.id "
        "AND (s.expires_at <= %s OR s.last_seen_at <= %s)) ORDER BY a.id FOR UPDATE",
        (now, idle_before),
    ).fetchall()
    for account_id, subject in expired:
        deleted = conn.execute(
            "DELETE FROM auth_sessions WHERE account_id = %s "
            "AND (expires_at <= %s OR last_seen_at <= %s)",
            (account_id, now, idle_before),
        ).rowcount
        if deleted:
            _audit(conn, "session_expired", "completed", subject=subject, account_id=account_id)
    conn.execute("DELETE FROM auth_links WHERE used_at IS NOT NULL OR expires_at <= %s", (now,))
    conn.execute("DELETE FROM auth_throttle WHERE failed_at < %s", (now - THROTTLE_RETENTION,))
