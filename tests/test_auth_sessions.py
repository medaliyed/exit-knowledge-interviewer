"""F19: cookie HTTP contract, CSRF, revocation and existing ACL boundaries."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from eki import accounts
from eki.access import set_owner, set_readers
from eki.api.accounts import Login, SetupPassword
from eki.api.app import create_app
from eki.auth import Principal, TokenAuthenticator
from eki.config import ConfigError, Settings
from eki.interview import store as interviews
from eki.runbook.service import RunbookService
from tests.conftest import Scratch
from tests.test_workflow_synthesis import finished

ORIGIN = "https://testserver"
HEADERS = {"Origin": ORIGIN}
GOOD = "correct horse battery staple 42"
NEW = "violet garden under winter stars 78"
EMAIL = "nadia@example.com"
TOKEN = "test-only-admin-bearer-credential-00000001"
COOKIE = "__Host-eki_session"


def configuration(dsn: str = "postgresql://unused") -> Settings:
    return Settings(_env_file=None, database_url=dsn, public_origin=ORIGIN)


def bearer() -> TokenAuthenticator:
    return TokenAuthenticator(
        json.dumps([{"token": TOKEN, "subject": "operator", "name": "Operator", "admin": True}])
    )


def test_auth_validation_and_models_do_not_echo_credentials() -> None:
    client = TestClient(create_app(settings=configuration(), authenticator=bearer()))
    for path in ("/auth/login", "/auth/setup-password", "/auth/password"):
        response = client.post(path, headers=HEADERS, json={"password": GOOD, "extra": GOOD})
        assert response.status_code in (401, 422)
        assert GOOD not in response.text
        assert response.headers["Cache-Control"] == "private, no-store"
    assert GOOD not in repr(Login(email=EMAIL, password=SecretStr(GOOD)))
    assert GOOD not in repr(SetupPassword(token=SecretStr(GOOD), new_password=SecretStr(GOOD)))


@pytest.mark.parametrize("value", ["not-an-address", "10.0.0.0/33", "*"])
def test_trusted_proxies_must_be_addresses_or_networks(value: str) -> None:
    with pytest.raises(ValueError, match="EKI_TRUSTED_PROXIES"):
        Settings(_env_file=None, database_url="postgresql://unused", trusted_proxies=value)


def test_public_auth_routes_require_exact_origin_and_json() -> None:
    client = TestClient(create_app(settings=configuration(), authenticator=bearer()))
    for path in ("/auth/login", "/auth/setup-password", "/auth/logout"):
        for origin in (None, "null", "https://evil.test", ORIGIN + ".evil.test"):
            response = client.post(path, headers={} if origin is None else {"Origin": origin})
            assert response.status_code == 403
            assert response.json()["code"] == "csrf_rejected"
            assert "Cookie" in response.headers["Vary"]
    response = client.post("/auth/login", headers=HEADERS, content='{"email":"x"}')
    assert response.status_code == 415
    assert client.post("/auth/logout", headers=HEADERS).status_code == 204


def test_bearer_session_adapter_and_cookie_precedence_need_no_account_store() -> None:
    client = TestClient(create_app(settings=configuration(), authenticator=bearer()))
    client.cookies.set(COOKIE, "invalid-session")
    response = client.get("/auth/session", headers={"Authorization": "Bearer " + TOKEN})
    assert response.status_code == 200
    assert response.json() == {
        "subject": "operator",
        "name": "Operator",
        "admin": True,
        "email": None,
    }
    assert client.get("/auth/session", headers={"Authorization": "Bearer wrong"}).status_code == 401


@pytest.fixture
def account_client(scratch: Scratch) -> TestClient:
    with scratch.connect() as conn:
        account = accounts.create_account(
            conn,
            email=EMAIL,
            name="Nadia Ferrand",
            subject="nadia",
            work_domains=frozenset({"example.com"}),
        )
        accounts.set_password(conn, account.id, GOOD, now=datetime.now(UTC))
    return TestClient(
        create_app(settings=configuration(scratch.dsn), authenticator=bearer()), base_url=ORIGIN
    )


def login(client: TestClient, *, email: str = EMAIL, password: str = GOOD):  # type: ignore[no-untyped-def]
    return client.post("/auth/login", headers=HEADERS, json={"email": email, "password": password})


@pytest.mark.db
class TestCookieHTTP:
    def test_login_cookie_restore_logout_and_secret_redaction(
        self, account_client: TestClient, scratch: Scratch
    ) -> None:
        response = login(account_client)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "subject": "nadia",
            "name": "Nadia Ferrand",
            "admin": False,
            "email": EMAIL,
        }
        cookie = response.headers["set-cookie"]
        assert all(
            value in cookie
            for value in (
                COOKIE,
                "HttpOnly",
                "Secure",
                "SameSite=strict",
                "Path=/",
                "Max-Age=604800",
            )
        )
        assert "Domain=" not in cookie
        raw = account_client.cookies.get(COOKIE)
        assert raw and raw not in response.text and GOOD not in response.text
        assert account_client.get("/auth/session").json() == response.json()
        restored = TestClient(account_client.app, base_url=ORIGIN)
        restored.cookies.update(account_client.cookies)
        assert restored.get("/auth/session").status_code == 200
        assert account_client.post("/auth/logout", headers=HEADERS).status_code == 204
        assert restored.get("/auth/session").status_code == 401
        with scratch.connect() as conn:
            logs = str(
                conn.execute(
                    "SELECT user_id, details FROM audit_events WHERE kind='auth'"
                ).fetchall()
            )
            assert all(secret not in logs for secret in (GOOD, EMAIL, raw))

    def test_login_failures_are_identical_and_throttle_is_durable(
        self, account_client: TestClient, scratch: Scratch
    ) -> None:
        unknown = login(account_client, email="unknown@example.com").content
        wrong = login(account_client, password="wrong").content
        with scratch.connect() as conn:
            accounts.create_account(
                conn,
                email="invite@example.com",
                name="Invite",
                subject="invite",
                work_domains=frozenset({"example.com"}),
            )
        unset = login(account_client, email="invite@example.com").content
        with scratch.connect() as conn:
            accounts.set_disabled(conn, EMAIL, True)
        disabled = login(account_client).content
        assert unknown == wrong == unset == disabled
        with scratch.connect() as conn:
            accounts.set_disabled(conn, EMAIL, False)
        for _ in range(3):
            assert login(account_client, password="wrong").status_code == 401
        throttled = login(account_client)
        assert throttled.status_code == 429
        assert 0 < int(throttled.headers["Retry-After"]) <= 60
        assert throttled.json()["code"] == "throttled"

    def test_throttle_counts_the_client_behind_a_trusted_proxy_only(self, scratch: Scratch) -> None:
        settings = Settings(
            _env_file=None,
            database_url=scratch.dsn,
            public_origin=ORIGIN,
            trusted_proxies="10.0.0.0/8, 192.0.2.1",
        )
        app = create_app(settings=settings, authenticator=bearer())
        proxied = TestClient(app, base_url=ORIGIN, client=("10.0.0.7", 443))
        direct = TestClient(app, base_url=ORIGIN, client=("198.51.100.9", 443))
        body = {"email": "ghost@example.com", "password": "wrong"}
        # The left-most entry is client-supplied and can be forged; the right-most
        # address that is not one of our proxies is the one the proxy actually saw.
        forwarded = {**HEADERS, "X-Forwarded-For": "1.2.3.4, 203.0.113.5, 10.0.0.3"}
        assert proxied.post("/auth/login", headers=forwarded, json=body).status_code == 401
        # A direct caller cannot choose its throttle key by sending the header itself.
        forged = {**HEADERS, "X-Forwarded-For": "203.0.113.77"}
        assert direct.post("/auth/login", headers=forged, json=body).status_code == 401
        with scratch.connect() as conn:
            keys = {r[0] for r in conn.execute("SELECT key FROM auth_throttle WHERE kind='ip'")}
        assert keys == {"203.0.113.5", "198.51.100.9"}

    def test_foreign_or_missing_origin_prevents_cookie_mutations(
        self, account_client: TestClient
    ) -> None:
        assert login(account_client).status_code == 200
        for headers in ({}, {"Origin": "https://evil.test"}):
            response = account_client.post(
                "/auth/password",
                headers=headers,
                json={"current_password": GOOD, "new_password": NEW},
            )
            assert response.status_code == 403
            assert (
                account_client.put(
                    "/runs/00000000-0000-0000-0000-000000000000/access",
                    headers=headers,
                    json={"readers": []},
                ).status_code
                == 403
            )
        assert account_client.get("/auth/session").status_code == 200

    def test_password_change_keeps_current_session_revokes_others_and_disable_revokes_it(
        self, account_client: TestClient, scratch: Scratch
    ) -> None:
        assert login(account_client).status_code == 200
        other = TestClient(account_client.app, base_url=ORIGIN)
        assert login(other).status_code == 200
        response = account_client.post(
            "/auth/password",
            headers=HEADERS,
            json={"current_password": "wrong", "new_password": NEW},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "current_password_incorrect"
        assert account_client.get("/auth/session").status_code == 200
        assert (
            account_client.post(
                "/auth/password",
                headers=HEADERS,
                json={"current_password": GOOD, "new_password": NEW},
            ).status_code
            == 204
        )
        assert account_client.get("/auth/session").status_code == 200
        assert other.get("/auth/session").status_code == 401
        assert login(other).status_code == 401
        assert login(other, password=NEW).status_code == 200
        with scratch.connect() as conn:
            accounts.set_disabled(conn, EMAIL, True)
        assert account_client.get("/auth/session").status_code == 401

    def test_setup_link_is_one_use_and_signs_in(self, scratch: Scratch) -> None:
        with scratch.connect() as conn:
            account = accounts.create_account(
                conn,
                email=EMAIL,
                name="Nadia Ferrand",
                subject="nadia",
                work_domains=frozenset({"example.com"}),
            )
            token = accounts.create_link(conn, account.id, "invite", now=datetime.now(UTC))
        client = TestClient(create_app(settings=configuration(scratch.dsn)), base_url=ORIGIN)
        response = client.post(
            "/auth/setup-password", headers=HEADERS, json={"token": token, "new_password": "short"}
        )
        assert response.status_code == 400 and response.json()["code"] == "weak_password"
        response = client.post(
            "/auth/setup-password", headers=HEADERS, json={"token": token, "new_password": GOOD}
        )
        assert response.status_code == 200
        assert token not in response.text
        assert client.get("/auth/session").status_code == 200
        used = client.post(
            "/auth/setup-password", headers=HEADERS, json={"token": token, "new_password": NEW}
        )
        unknown = client.post(
            "/auth/setup-password", headers=HEADERS, json={"token": "unknown", "new_password": NEW}
        )
        assert used.content == unknown.content and used.status_code == 400

    def test_startup_refuses_bearer_account_subject_conflict(self, scratch: Scratch) -> None:
        with scratch.connect() as conn:
            accounts.create_account(
                conn,
                email=EMAIL,
                name="Conflicting name",
                subject="operator",
                work_domains=frozenset({"example.com"}),
            )
        with (
            pytest.raises(ConfigError),
            TestClient(create_app(settings=configuration(scratch.dsn), authenticator=bearer())),
        ):
            pass

    @pytest.mark.anyio
    async def test_cookie_identities_obey_reader_owner_and_approval_rules(
        self, scratch: Scratch
    ) -> None:
        session = await finished(scratch)
        books = RunbookService(scratch.dsn)
        first, draft, _ = books.list(session)
        approved = books.approve(UUID(first.entry.id), "Nadia Ferrand")
        with scratch.connect() as conn:
            view = interviews.load_session(conn, session)
            assert view is not None
            set_owner(conn, view.run_id, "nadia", Principal("operator", "Operator", True))
            set_readers(conn, view.run_id, ["reader"], Principal("nadia", "Nadia Ferrand"))
            for subject, name in (
                ("nadia", "Nadia Ferrand"),
                ("reader", "Reader"),
                ("stranger", "Stranger"),
            ):
                account = accounts.create_account(
                    conn,
                    email=subject + "@example.com",
                    name=name,
                    subject=subject,
                    work_domains=frozenset({"example.com"}),
                )
                accounts.set_password(conn, account.id, GOOD, now=datetime.now(UTC))
        app = create_app(
            runbook_service=books, settings=configuration(scratch.dsn), authenticator=bearer()
        )
        for subject, status in (("reader", 200), ("nadia", 200), ("stranger", 404)):
            client = TestClient(app, base_url=ORIGIN)
            assert login(client, email=subject + "@example.com").status_code == 200
            response = client.get(f"/entries/{approved.entry.id}")
            assert response.status_code == status
            if subject == "reader":
                assert "decisions" not in response.json()
                for path in (
                    f"/sessions/{session}",
                    f"/plans/{view.plan_id}",
                    f"/entries/{draft.entry.id}",
                ):
                    assert client.get(path).status_code == 404
            if subject == "nadia":
                assert (
                    client.post(
                        f"/entries/{draft.entry.id}/approve",
                        headers=HEADERS,
                        json={"approved_by": "Someone Else"},
                    ).status_code
                    == 422
                )
                assert (
                    client.post(
                        f"/entries/{draft.entry.id}/approve",
                        headers=HEADERS,
                        json={"approved_by": "Nadia Ferrand"},
                    ).status_code
                    == 200
                )


@pytest.mark.db
def test_startup_logs_pending_migrations(
    scratch: Scratch,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with TestClient(create_app(settings=configuration(scratch.dsn), authenticator=bearer())):
        pass
    assert "eki migrate" not in caplog.text
    (tmp_path / "9999_future.sql").write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr("eki.db.MIGRATIONS_DIR", tmp_path)
    with TestClient(create_app(settings=configuration(scratch.dsn), authenticator=bearer())):
        pass
    assert "1 database migration is pending (9999_future.sql); run: eki migrate" in caplog.text
