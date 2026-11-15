"""F19: accounts, passwords, invitation links, sessions and throttling (Phase 6, 6a)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from pydantic import SecretStr

from eki import accounts
from eki.accounts import (
    LinkError,
    PasswordPolicyError,
    SessionPolicy,
    SignInRefused,
    Throttled,
    check_password,
    hash_password,
    needs_rehash,
    normalise_email,
    verify_password,
)
from eki.audit import audit_context, list_auth_events
from eki.auth import Principal
from eki.config import ConfigError
from tests.conftest import Scratch
from tests.interview_support import seed_run

NOW = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
DOMAINS = frozenset({"example.com"})
GOOD = "correct horse battery staple 42"
POLICY = SessionPolicy(idle=timedelta(hours=8), max_age=timedelta(days=7))


def test_email_is_trimmed_and_lower_cased() -> None:
    assert normalise_email("  Nadia.Ferrand@Example.COM ") == "nadia.ferrand@example.com"
    for bad in ("", "nadia", "nadia@", "@example.com", "a b@example.com", "x" * 250 + "@e.com"):
        with pytest.raises(ValueError):
            normalise_email(bad)


@pytest.mark.parametrize(
    ("password", "code"),
    [
        ("short pass1", "too_short"),
        ("x" * 129, "too_long"),
        ("password1234", "common"),
        ("Nadia.Ferrand rocks!", "contains_identity"),
        ("nadia is here 2026", "contains_identity"),
        ("ferrand's secret pin", "contains_identity"),
        ("long password\x00 controls", "not_printable"),
    ],
)
def test_password_policy_refuses_weak_or_identifying_passwords(password: str, code: str) -> None:
    with pytest.raises(PasswordPolicyError) as error:
        check_password(password, email="nadia.ferrand@example.com", name="Nadia Ferrand")
    assert error.value.code == code
    assert password not in str(error.value)


def test_password_policy_accepts_long_phrases_and_refuses_an_unchanged_one() -> None:
    check_password(GOOD, email="nadia.ferrand@example.com", name="Nadia Ferrand")
    check_password("ünïcödé passphrase ✓✓", email="n@example.com", name="N")
    with pytest.raises(PasswordPolicyError) as error:
        check_password(GOOD, email="n@example.com", name="N", current_hash=hash_password(GOOD))
    assert error.value.code == "unchanged"


def test_hashes_are_salted_scrypt_and_upgrade_when_parameters_change() -> None:
    first, second = hash_password(GOOD), hash_password(GOOD)
    assert first != second and first.startswith("scrypt$")
    assert GOOD not in first
    assert verify_password(GOOD, first) and verify_password(GOOD, second)
    assert not verify_password(GOOD + "x", first)
    assert not verify_password(GOOD, "garbage")
    assert not needs_rehash(first)
    weaker = first.replace("$32768$", "$16384$", 1)
    assert needs_rehash(weaker)
    assert needs_rehash("garbage")


def test_password_secrets_and_session_ids_do_not_appear_in_representations() -> None:
    password = SecretStr(GOOD)
    check_password(password, email="n@example.com", name="Nadia Ferrand")
    stored = hash_password(password)
    assert verify_password(password, stored)
    assert GOOD not in repr(password)
    signed = accounts.Signed(Principal("nadia", "Nadia Ferrand"), "sensitive-session-id")
    assert signed.session_id not in repr(signed)


@pytest.mark.db
class TestAccountStore:
    def test_create_enforces_work_domain_and_uniqueness(self, conn: psycopg.Connection) -> None:
        account = accounts.create_account(
            conn,
            email="Nadia.Ferrand@Example.com",
            name="Nadia Ferrand",
            subject="nadia",
            work_domains=DOMAINS,
        )
        assert (account.email, account.subject, account.admin, account.has_password) == (
            "nadia.ferrand@example.com",
            "nadia",
            False,
            False,
        )
        with pytest.raises(ValueError, match="work domain"):
            accounts.create_account(
                conn, email="sam@gmail.com", name="Sam", subject="sam", work_domains=DOMAINS
            )
        external = accounts.create_account(
            conn,
            email="sam@gmail.com",
            name="Sam",
            subject="sam",
            work_domains=DOMAINS,
            allow_external_domain=True,
        )
        assert external.email == "sam@gmail.com"
        for email, subject in (
            ("nadia.ferrand@example.com", "other"),
            ("new@example.com", "nadia"),
        ):
            with pytest.raises(accounts.AccountExistsError):
                accounts.create_account(
                    conn, email=email, name="X", subject=subject, work_domains=DOMAINS
                )
        assert [a.subject for a in accounts.list_accounts(conn)] == ["nadia", "sam"]
        kinds = conn.execute(
            "SELECT kind, phase, details->>'operation', user_id FROM audit_events ORDER BY id"
        ).fetchall()
        assert ("auth", "completed", "account_created", "nadia") in kinds
        text = str(conn.execute("SELECT details FROM audit_events").fetchall())
        assert "example.com" not in text

    def test_create_refuses_a_subject_that_conflicts_with_a_bearer_identity(
        self, conn: psycopg.Connection
    ) -> None:
        from eki.auth import Principal

        with pytest.raises(ConfigError):
            accounts.create_account(
                conn,
                email="n@example.com",
                name="Nadia Ferrand",
                subject="nadia",
                work_domains=DOMAINS,
                bearer_identities=[Principal("nadia", "Someone Else")],
            )
        accounts.create_account(
            conn,
            email="n@example.com",
            name="Nadia Ferrand",
            subject="nadia",
            work_domains=DOMAINS,
            bearer_identities=[Principal("nadia", "Nadia Ferrand")],
        )

    def test_invitation_link_sets_password_once_and_signs_in(
        self, conn: psycopg.Connection
    ) -> None:
        account = accounts.create_account(
            conn, email="n@example.com", name="Nadia Ferrand", subject="nadia", work_domains=DOMAINS
        )
        superseded = accounts.create_link(conn, account.id, "invite", now=NOW)
        token = accounts.create_link(conn, account.id, "invite", now=NOW)
        assert len(token) >= 40 and token != superseded
        stored = conn.execute("SELECT token_hash FROM auth_links").fetchall()
        assert all(token.encode() != row[0] for row in stored)
        with pytest.raises(LinkError):
            accounts.redeem_link(conn, superseded, GOOD, now=NOW, policy=POLICY)
        with pytest.raises(PasswordPolicyError):
            accounts.redeem_link(conn, token, "password1234", now=NOW, policy=POLICY)
        with pytest.raises(LinkError):
            accounts.redeem_link(conn, token, GOOD, now=NOW + timedelta(hours=73), policy=POLICY)
        signed = accounts.redeem_link(conn, token, GOOD, now=NOW, policy=POLICY)
        assert signed.principal.subject == "nadia" and signed.session_id
        assert accounts.get_account(conn, "n@example.com").has_password
        with pytest.raises(LinkError):
            accounts.redeem_link(conn, token, GOOD, now=NOW, policy=POLICY)
        with pytest.raises(LinkError):
            accounts.redeem_link(conn, "not-a-token", GOOD, now=NOW, policy=POLICY)
        reset = accounts.create_link(conn, account.id, "reset", now=NOW)
        with pytest.raises(LinkError):
            accounts.redeem_link(conn, reset, GOOD, now=NOW + timedelta(minutes=61), policy=POLICY)

    def test_sign_in_is_generic_and_counts_failures(self, conn: psycopg.Connection) -> None:
        account = accounts.create_account(
            conn, email="n@example.com", name="Nadia Ferrand", subject="nadia", work_domains=DOMAINS
        )
        with pytest.raises(SignInRefused):  # no password yet
            accounts.sign_in(conn, "n@example.com", GOOD, now=NOW, client="1.1.1.1", policy=POLICY)
        accounts.set_password(conn, account.id, GOOD, now=NOW)
        with pytest.raises(SignInRefused):
            accounts.sign_in(
                conn, "nobody@example.com", GOOD, now=NOW, client="1.1.1.1", policy=POLICY
            )
        with pytest.raises(SignInRefused):
            accounts.sign_in(
                conn, "n@example.com", "wrong", now=NOW, client="1.1.1.1", policy=POLICY
            )
        signed = accounts.sign_in(
            conn, " N@Example.com ", GOOD, now=NOW, client="1.1.1.1", policy=POLICY, user_agent="UA"
        )
        assert signed.principal.name == "Nadia Ferrand"
        accounts.set_disabled(conn, "n@example.com", True)
        with pytest.raises(SignInRefused):
            accounts.sign_in(conn, "n@example.com", GOOD, now=NOW, client="1.1.1.1", policy=POLICY)
        assert (
            accounts.authenticate_session(conn, signed.session_id, now=NOW, policy=POLICY) is None
        )
        rows = conn.execute(
            "SELECT phase, details->>'operation', user_id FROM audit_events "
            "WHERE kind='auth' AND details->>'operation'='sign_in' ORDER BY id"
        ).fetchall()
        assert rows[0] == ("failed", "sign_in", "nadia")
        assert rows[1] == ("failed", "sign_in", "anonymous")
        assert ("completed", "sign_in", "nadia") in rows
        assert ("refused", "sign_in", "nadia") in rows

    def test_throttling_by_email_backs_off_and_by_ip_blocks(self, conn: psycopg.Connection) -> None:
        account = accounts.create_account(
            conn, email="n@example.com", name="Nadia Ferrand", subject="nadia", work_domains=DOMAINS
        )
        accounts.set_password(conn, account.id, GOOD, now=NOW)
        for _ in range(5):
            with pytest.raises(SignInRefused):
                accounts.sign_in(
                    conn, "n@example.com", "wrong", now=NOW, client="1.1.1.1", policy=POLICY
                )
        with pytest.raises(Throttled) as throttled:
            accounts.sign_in(conn, "n@example.com", GOOD, now=NOW, client="1.1.1.1", policy=POLICY)
        assert 0 < throttled.value.retry_after <= 60
        # A refused attempt during the backoff is not another failure.
        with pytest.raises(Throttled):
            accounts.sign_in(conn, "n@example.com", GOOD, now=NOW, client="2.2.2.2", policy=POLICY)
        later = NOW + timedelta(seconds=61)
        with pytest.raises(SignInRefused):
            accounts.sign_in(
                conn, "n@example.com", "wrong", now=later, client="1.1.1.1", policy=POLICY
            )
        with pytest.raises(Throttled) as doubled:
            accounts.sign_in(
                conn, "n@example.com", GOOD, now=later, client="1.1.1.1", policy=POLICY
            )
        assert 60 < doubled.value.retry_after <= 120
        assert accounts.sign_in(
            conn,
            "n@example.com",
            GOOD,
            now=later + timedelta(minutes=16),
            client="1.1.1.1",
            policy=POLICY,
        ).session_id
        # Unknown addresses are throttled the same way, so the limit reveals nothing.
        for _ in range(5):
            with pytest.raises(SignInRefused):
                accounts.sign_in(
                    conn, "ghost@example.com", "x", now=NOW, client="3.3.3.3", policy=POLICY
                )
        with pytest.raises(Throttled):
            accounts.sign_in(
                conn, "ghost@example.com", "x", now=NOW, client="3.3.3.3", policy=POLICY
            )
        for index in range(50):
            with pytest.raises(SignInRefused):
                accounts.sign_in(
                    conn, f"u{index}@example.com", "x", now=NOW, client="9.9.9.9", policy=POLICY
                )
        with pytest.raises(Throttled) as blocked:
            accounts.sign_in(conn, "n@example.com", GOOD, now=NOW, client="9.9.9.9", policy=POLICY)
        assert blocked.value.retry_after == 900

    def test_sessions_expire_idle_and_absolute_and_touch_sparingly(
        self, conn: psycopg.Connection
    ) -> None:
        account = accounts.create_account(
            conn, email="n@example.com", name="Nadia Ferrand", subject="nadia", work_domains=DOMAINS
        )
        accounts.set_password(conn, account.id, GOOD, now=NOW)
        signed = accounts.sign_in(
            conn, "n@example.com", GOOD, now=NOW, client="1.1.1.1", policy=POLICY
        )
        assert conn.execute(
            "SELECT count(*) FROM auth_sessions WHERE id_hash=%s", (signed.session_id.encode(),)
        ).fetchone() == (0,)
        principal = accounts.authenticate_session(
            conn, signed.session_id, now=NOW + timedelta(seconds=30), policy=POLICY
        )
        assert principal is not None and principal.subject == "nadia"
        seen = conn.execute("SELECT last_seen_at FROM auth_sessions").fetchone()
        assert seen == (NOW,)  # not touched within a minute
        accounts.authenticate_session(
            conn, signed.session_id, now=NOW + timedelta(minutes=2), policy=POLICY
        )
        assert conn.execute("SELECT last_seen_at FROM auth_sessions").fetchone() == (
            NOW + timedelta(minutes=2),
        )
        assert (
            accounts.authenticate_session(
                conn, signed.session_id, now=NOW + timedelta(hours=10, minutes=3), policy=POLICY
            )
            is None
        )
        again = accounts.sign_in(
            conn, "n@example.com", GOOD, now=NOW, client="1.1.1.1", policy=POLICY
        )
        assert again.session_id != signed.session_id
        at = NOW
        while at < NOW + timedelta(days=7):
            at += timedelta(hours=7)
            accounts.authenticate_session(conn, again.session_id, now=at, policy=POLICY)
        assert accounts.authenticate_session(conn, again.session_id, now=at, policy=POLICY) is None
        assert accounts.authenticate_session(conn, "unknown", now=NOW, policy=POLICY) is None

    def test_password_change_keeps_this_session_and_revokes_others(
        self, conn: psycopg.Connection
    ) -> None:
        account = accounts.create_account(
            conn, email="n@example.com", name="Nadia Ferrand", subject="nadia", work_domains=DOMAINS
        )
        accounts.set_password(conn, account.id, GOOD, now=NOW)
        laptop = accounts.sign_in(
            conn, "n@example.com", GOOD, now=NOW, client="1.1.1.1", policy=POLICY
        )
        phone = accounts.sign_in(
            conn, "n@example.com", GOOD, now=NOW, client="1.1.1.2", policy=POLICY
        )
        with pytest.raises(SignInRefused):
            accounts.change_password(
                conn, laptop.session_id, "wrong", GOOD + "!", now=NOW, policy=POLICY
            )
        with pytest.raises(PasswordPolicyError):
            accounts.change_password(conn, laptop.session_id, GOOD, GOOD, now=NOW, policy=POLICY)
        accounts.change_password(conn, laptop.session_id, GOOD, GOOD + "!", now=NOW, policy=POLICY)
        assert (
            accounts.authenticate_session(conn, laptop.session_id, now=NOW, policy=POLICY)
            is not None
        )
        assert accounts.authenticate_session(conn, phone.session_id, now=NOW, policy=POLICY) is None
        with pytest.raises(SignInRefused):
            accounts.sign_in(conn, "n@example.com", GOOD, now=NOW, client="1.1.1.1", policy=POLICY)
        accounts.sign_out(conn, laptop.session_id)
        assert (
            accounts.authenticate_session(conn, laptop.session_id, now=NOW, policy=POLICY) is None
        )
        accounts.revoke_sessions(conn, account.id)

    def test_a_successful_sign_in_upgrades_an_older_password_hash(
        self, conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        account = accounts.create_account(
            conn, email="n@example.com", name="Nadia Ferrand", subject="nadia", work_domains=DOMAINS
        )
        with monkeypatch.context() as older:
            older.setattr(accounts, "SCRYPT_N", 16384)
            accounts.set_password(conn, account.id, GOOD, now=NOW)
        previous = conn.execute("SELECT password_hash FROM accounts").fetchone()
        assert previous is not None and needs_rehash(previous[0])
        accounts.sign_in(conn, account.email, GOOD, now=NOW, client="test-ip", policy=POLICY)
        current = conn.execute("SELECT password_hash FROM accounts").fetchone()
        assert current is not None and not needs_rehash(current[0])
        assert verify_password(GOOD, current[0])

    def test_prune_removes_expired_sessions_used_links_and_old_throttle_rows(
        self, conn: psycopg.Connection
    ) -> None:
        account = accounts.create_account(
            conn, email="n@example.com", name="Nadia Ferrand", subject="nadia", work_domains=DOMAINS
        )
        token = accounts.create_link(conn, account.id, "invite", now=NOW)
        accounts.redeem_link(conn, token, GOOD, now=NOW, policy=POLICY)
        with pytest.raises(SignInRefused):
            accounts.sign_in(
                conn, "n@example.com", "wrong", now=NOW, client="1.1.1.1", policy=POLICY
            )
        accounts.prune(conn, now=NOW + timedelta(days=8))
        counts = tuple(
            conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608 - fixed names
            for table in ("auth_sessions", "auth_links", "auth_throttle")
        )
        assert counts == ((0,), (0,), (0,))

    def test_secrets_never_reach_audit_rows(self, conn: psycopg.Connection) -> None:
        account = accounts.create_account(
            conn, email="n@example.com", name="Nadia Ferrand", subject="nadia", work_domains=DOMAINS
        )
        token = accounts.create_link(conn, account.id, "invite", now=NOW)
        signed = accounts.redeem_link(conn, token, GOOD, now=NOW, policy=POLICY)
        with audit_context(user_id="should-not-leak-through"):
            accounts.sign_out(conn, signed.session_id)
        rows = conn.execute("SELECT user_id, details::text FROM audit_events").fetchall()
        joined = " ".join(f"{user} {details}" for user, details in rows)
        for secret in (GOOD, token, signed.session_id, "n@example.com"):
            assert secret not in joined
        assert "should-not-leak-through" not in joined

    def test_subject_consistency_and_auth_audit_pagination(self, conn: psycopg.Connection) -> None:
        account = accounts.create_account(
            conn, email="n@example.com", name="Nadia Ferrand", subject="nadia", work_domains=DOMAINS
        )
        assert accounts.get_account_by_subject(conn, "nadia") == account
        accounts.validate_bearer_identities(conn, [account.principal])
        with pytest.raises(ConfigError):
            accounts.validate_bearer_identities(conn, [Principal("nadia", "Other")])
        with pytest.raises(ConfigError):
            accounts.validate_bearer_identities(conn, [Principal("nadia", account.name, True)])
        accounts.create_link(conn, account.id, "invite", now=NOW)
        page = list_auth_events(conn, limit=1)
        assert len(page) == 1 and page[0]["run_id"] is None
        assert isinstance(page[0]["id"], int)
        assert len(list_auth_events(conn, after=page[0]["id"])) == 1

    def test_updates_keep_subject_domains_bearer_and_approval_identity(
        self, conn: psycopg.Connection
    ) -> None:
        account = accounts.create_account(
            conn, email="n@example.com", name="Nadia Ferrand", subject="nadia", work_domains=DOMAINS
        )
        updated = accounts.update_account(
            conn,
            account.email,
            new_email=" New@Example.com ",
            name="Nadia Ferrand",
            work_domains=DOMAINS,
            bearer_identities=(account.principal,),
        )
        assert updated.email == "new@example.com" and updated.subject == account.subject
        with pytest.raises(ValueError, match="work domain"):
            accounts.update_account(
                conn, updated.email, new_email="external@elsewhere.com", work_domains=DOMAINS
            )
        with pytest.raises(ConfigError):
            accounts.update_account(
                conn,
                updated.email,
                name="Other",
                work_domains=DOMAINS,
                bearer_identities=(account.principal,),
            )
        run = seed_run(conn, mapped=False)
        conn.execute("UPDATE ingest_runs SET owner_subject=%s WHERE id=%s", (account.subject, run))
        conn.execute(
            "INSERT INTO interview_plans (id,run_id,status,approved_by,approved_at) "
            "VALUES (%s,%s,'approved',%s,%s)",
            (uuid4(), run, account.name, NOW),
        )
        with pytest.raises(ValueError, match="reviewer must match"):
            accounts.update_account(conn, updated.email, name="Other", work_domains=DOMAINS)
        assert accounts.get_account(conn, updated.email).name == account.name
        assert any(
            event["details"] == {"operation": "account_updated", "target_id": str(account.id)}
            for event in list_auth_events(conn)
        )

    def test_failed_attempts_commit_and_concurrent_attempts_obey_limit(
        self, scratch: Scratch
    ) -> None:
        barrier = Barrier(8)

        def attempt(index: int) -> str:
            with scratch.connect() as connection:
                barrier.wait(timeout=10)
                try:
                    accounts.sign_in(
                        connection,
                        "unknown@example.com",
                        "wrong",
                        now=NOW,
                        client=f"client-{index}",
                        policy=POLICY,
                    )
                except Throttled:
                    return "throttled"
                except SignInRefused:
                    return "failed"
            raise AssertionError("an unknown identity cannot sign in")

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(8)))
        assert outcomes.count("failed") == 5
        assert outcomes.count("throttled") == 3
        with scratch.connect() as connection:
            assert connection.execute(
                "SELECT count(*) FROM auth_throttle WHERE kind='email'"
            ).fetchone() == (5,)
            assert len(list_auth_events(connection)) == 8

    def test_a_link_is_redeemed_once_under_concurrent_requests(self, scratch: Scratch) -> None:
        with scratch.connect() as connection:
            account = accounts.create_account(
                connection,
                email="n@example.com",
                name="Nadia Ferrand",
                subject="nadia",
                work_domains=DOMAINS,
            )
            token = accounts.create_link(connection, account.id, "invite", now=NOW)
        barrier = Barrier(2)

        def redeem(index: int) -> str:
            with scratch.connect() as connection:
                barrier.wait(timeout=10)
                try:
                    accounts.redeem_link(
                        connection, token, GOOD, now=NOW, policy=POLICY, client=f"client-{index}"
                    )
                except LinkError:
                    return "invalid"
                return "signed-in"

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(redeem, range(2))) == ["invalid", "signed-in"]
        with scratch.connect() as connection:
            assert connection.execute("SELECT count(*) FROM auth_sessions").fetchone() == (1,)

    def test_link_endpoints_share_the_sign_in_ip_limit(self, scratch: Scratch) -> None:
        with scratch.connect() as connection:
            for _ in range(50):
                with pytest.raises(LinkError):
                    accounts.redeem_link(
                        connection, "unknown-token", GOOD, now=NOW, policy=POLICY, client="test-ip"
                    )
            with pytest.raises(Throttled) as blocked:
                accounts.redeem_link(
                    connection, "unknown-token", GOOD, now=NOW, policy=POLICY, client="test-ip"
                )
            assert blocked.value.retry_after == 900
            with pytest.raises(Throttled):
                accounts.sign_in(
                    connection, "n@example.com", GOOD, now=NOW, policy=POLICY, client="test-ip"
                )
