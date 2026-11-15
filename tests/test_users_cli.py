"""F19: trusted operators provision accounts and revoke browser access without a restart."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from eki import accounts
from eki.cli import _parser, main
from eki.config import get_settings
from tests.conftest import Scratch


def test_auth_audit_selection_is_explicit_and_mutually_exclusive() -> None:
    args = _parser().parse_args(["audit", "--auth", "--after", "7"])
    assert args.auth and args.run is None and args.after == 7
    for flags in ([], ["--auth", "--run", "12b73503-58cd-4b24-bb19-ea0ae1217601"]):
        with pytest.raises(SystemExit):
            _parser().parse_args(["audit", *flags])


@pytest.fixture
def users_database(scratch: Scratch, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("EKI_DATABASE_URL", scratch.dsn)
    monkeypatch.setenv("EKI_PUBLIC_ORIGIN", "http://localhost:8000")
    monkeypatch.setenv("EKI_API_TOKENS", "[]")
    monkeypatch.setenv("EKI_WORK_EMAIL_DOMAINS", "example.com")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.db
def test_operator_provision_reset_disable_enable_and_revoke(
    users_database: None, scratch: Scratch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "users",
                "add",
                "--email",
                " Nadia@Example.COM ",
                "--name",
                "Nadia Ferrand",
                "--subject",
                "nadia",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    link = urlsplit(output.strip().splitlines()[-1])
    assert link.path == "/workspace/set-password" and not link.query
    invite = parse_qs(link.fragment)["token"][0]
    assert main(["users", "list"]) == 0
    listing = capsys.readouterr().out
    assert "nadia@example.com" in listing and "Nadia Ferrand" in listing
    assert invite not in listing
    with scratch.connect() as conn:
        account = accounts.get_account(conn, "nadia@example.com")
        assert not account.disabled
        accounts.set_password(
            conn, account.id, "correct horse battery staple 42", now=datetime.now(UTC)
        )
    assert main(["users", "reset-password", "--email", "nadia@example.com"]) == 0
    reset = parse_qs(urlsplit(capsys.readouterr().out.strip().splitlines()[-1]).fragment)["token"][
        0
    ]
    assert reset != invite
    assert main(["users", "disable", "--email", "nadia@example.com"]) == 0
    with scratch.connect() as conn:
        assert accounts.get_account(conn, "nadia@example.com").disabled
    assert main(["users", "enable", "--email", "nadia@example.com"]) == 0
    assert main(["users", "revoke-sessions", "--email", "nadia@example.com"]) == 0
    capsys.readouterr()
    assert main(["audit", "--auth"]) == 0
    events = json.loads(capsys.readouterr().out)
    operations = {event["details"]["operation"] for event in events}
    assert {
        "account_created",
        "link_created",
        "account_disabled",
        "account_enabled",
        "sessions_revoked",
    } <= operations
    assert all(event["run_id"] is None for event in events)
    serialised = json.dumps(events)
    for secret in (invite, reset, "nadia@example.com", "correct horse battery staple 42"):
        assert secret not in serialised


@pytest.mark.db
def test_add_refuses_conflicts_and_external_domains_before_creating_account(
    users_database: None,
    scratch: Scratch,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = [
        "users",
        "add",
        "--email",
        "n@elsewhere.com",
        "--name",
        "Nadia",
        "--subject",
        "nadia",
    ]
    assert main(arguments) == 1
    assert "work domain" in capsys.readouterr().err
    monkeypatch.setenv(
        "EKI_API_TOKENS",
        json.dumps(
            [
                {"token": "x" * 40, "subject": "nadia", "name": "Other Name"},
            ]
        ),
    )
    get_settings.cache_clear()
    assert main([*arguments, "--allow-external-domain"]) == 2
    assert "different name" in capsys.readouterr().err
    with scratch.connect() as conn:
        assert accounts.list_accounts(conn) == []
    monkeypatch.setenv("EKI_API_TOKENS", "[]")
    get_settings.cache_clear()
    assert main([*arguments, "--allow-external-domain"]) == 0
    capsys.readouterr()


@pytest.mark.db
def test_update_keeps_subject_and_checks_email_and_bearer_identity(
    users_database: None,
    scratch: Scratch,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [
                "users",
                "add",
                "--email",
                "nadia@example.com",
                "--name",
                "Nadia Ferrand",
                "--subject",
                "nadia",
            ]
        )
        == 0
    )
    capsys.readouterr()
    update = ["users", "update", "--email", "nadia@example.com"]
    assert main(update) == 1
    assert main([*update, "--new-email", "new@elsewhere.com"]) == 1
    assert "work domain" in capsys.readouterr().err
    assert main([*update, "--new-email", " NEW@Example.COM "]) == 0
    capsys.readouterr()
    monkeypatch.setenv(
        "EKI_API_TOKENS",
        json.dumps(
            [
                {"token": "x" * 40, "subject": "nadia", "name": "Nadia Ferrand"},
            ]
        ),
    )
    get_settings.cache_clear()
    assert main(["users", "update", "--email", "new@example.com", "--name", "Different Name"]) == 2
    capsys.readouterr()
    with scratch.connect() as conn:
        account = accounts.get_account(conn, "new@example.com")
        assert account.subject == "nadia" and account.name == "Nadia Ferrand" and not account.admin
