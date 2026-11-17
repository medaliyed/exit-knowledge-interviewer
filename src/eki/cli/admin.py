"""Local-operator commands: browser accounts, access, maintenance, handover and audit."""

import argparse
import json
from datetime import UTC, datetime

import psycopg

from eki import access, accounts, maintenance
from eki.audit import (
    list_auth_events,
    list_events,
)
from eki.auth import (
    Principal,
    TokenAuthenticator,
)
from eki.config import ConfigError, Settings, work_domains


def cmd_users(conn: psycopg.Connection, arguments: argparse.Namespace, settings: Settings) -> int:
    command = arguments.user_command
    if command == "list":
        print("Email\tSubject\tName\tAdmin\tDisabled\tLast sign-in")
        for account in accounts.list_accounts(conn):
            last_sign_in = (
                account.last_sign_in_at.isoformat() if account.last_sign_in_at else "never"
            )
            print(
                f"{account.email}\t{account.subject}\t{account.name}\t"
                f"{str(account.admin).lower()}\t{str(account.disabled).lower()}\t{last_sign_in}"
            )
        return 0
    if command == "update":
        if arguments.new_email is None and arguments.name is None:
            raise ValueError("supply --new-email or --name to update an account")
        account = accounts.update_account(
            conn,
            arguments.email,
            new_email=arguments.new_email,
            name=arguments.name,
            work_domains=work_domains(settings),
            allow_external_domain=arguments.allow_external_domain,
            bearer_identities=TokenAuthenticator(settings.api_tokens.get_secret_value()).identities,
        )
        print(f"Account {account.subject} updated.")
        return 0
    if command in ("add", "reset-password"):
        if not settings.public_origin:
            raise ConfigError("set EKI_PUBLIC_ORIGIN before creating an invitation or reset link")
        now = datetime.now(UTC)
        # Account creation and its invitation either both commit or neither does.
        with conn.transaction():
            if command == "add":
                account = accounts.create_account(
                    conn,
                    email=arguments.email,
                    name=arguments.name,
                    subject=arguments.subject,
                    admin=arguments.admin,
                    allow_external_domain=arguments.allow_external_domain,
                    work_domains=work_domains(settings),
                    bearer_identities=list(
                        TokenAuthenticator(settings.api_tokens.get_secret_value()).identities
                    ),
                    now=now,
                )
            else:
                account = accounts.get_account(conn, arguments.email)
            purpose: accounts.Purpose = "invite" if command == "add" else "reset"
            token = accounts.create_link(conn, account.id, purpose, now=now)
        # Do not print a usable link until the transaction has committed.
        conn.commit()
        print(
            "Invitation link (valid for 72 hours):"
            if command == "add"
            else "Reset link (valid for 1 hour):"
        )
        print(f"{settings.public_origin.rstrip('/')}/workspace/set-password#token={token}")
        return 0
    if command in ("disable", "enable"):
        account = accounts.set_disabled(conn, arguments.email, command == "disable")
        print(f"Account {account.subject} {'disabled' if account.disabled else 'enabled'}.")
        return 0
    account = accounts.get_account(conn, arguments.email)
    count = accounts.revoke_sessions(conn, account.id)
    print(f"Revoked {count} session(s) for {account.subject}.")
    return 0


def execute(conn: psycopg.Connection, arguments: argparse.Namespace, settings: Settings) -> int:
    """``eki audit``, ``maintenance``, ``handover`` and ``access``."""
    if arguments.command == "audit":
        print(
            json.dumps(
                list_auth_events(conn, after=arguments.after, limit=arguments.limit)
                if arguments.auth
                else list_events(conn, arguments.run, after=arguments.after, limit=arguments.limit)
            )
        )
        return 0
    if arguments.command == "maintenance":
        operator = Principal("local-operator", "Local operator", True)
        print(
            json.dumps(
                {
                    "run": maintenance.state(conn, arguments.run, operator),
                    **maintenance.queue(
                        conn,
                        arguments.run,
                        operator,
                        filter=arguments.filter,
                        offset=arguments.offset,
                    ),
                },
                default=str,
            )
        )
        return 0
    if arguments.command == "handover":
        state = maintenance.transfer(
            conn,
            arguments.run,
            arguments.to,
            Principal("local-operator", "Local operator", True),
            expected_owner=arguments.expected_owner,
            expected_revision=arguments.expected_run_revision,
            reason=arguments.reason,
            start=True,
            identities=TokenAuthenticator(settings.api_tokens.get_secret_value()).identities,
        )
        print(json.dumps(state, default=str))
        return 0
    if arguments.command == "access":
        operator = Principal("local-operator", "Local operator", True)
        if arguments.owner is not None:
            access.set_owner(
                conn,
                arguments.run,
                arguments.owner,
                operator,
                expected_owner=arguments.expected_owner,
                expected_revision=arguments.expected_run_revision,
                reason=arguments.reason,
                identities=TokenAuthenticator(settings.api_tokens.get_secret_value()).identities,
            )
        if arguments.readers is not None:
            access.set_readers(conn, arguments.run, arguments.readers, operator)
        print(json.dumps(access.readers(conn, arguments.run, operator)))
        return 0
    raise ValueError(f"unknown command {arguments.command}")
