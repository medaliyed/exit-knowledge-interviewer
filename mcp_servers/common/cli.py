"""Command line shared by the servers: the scope is fixed at start-up."""

import argparse
from datetime import UTC, datetime
from pathlib import Path

from mcp_servers.common.source import Scope


def _day(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def parse_arguments(description: str, argv: list[str] | None = None) -> tuple[Scope, Path]:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--expert-name", required=True)
    parser.add_argument("--expert-email", required=True)
    parser.add_argument("--since", required=True, help="first day, YYYY-MM-DD")
    parser.add_argument("--until", required=True, help="last day, YYYY-MM-DD, inclusive")
    parser.add_argument("--work-domain", action="append", required=True, dest="domains")
    arguments = parser.parse_args(argv)
    end_of_day = _day(arguments.until).replace(hour=23, minute=59, second=59)
    scope = Scope(
        expert_name=arguments.expert_name,
        expert_email=arguments.expert_email,
        since=_day(arguments.since),
        until=end_of_day,
        work_email_domains=frozenset(arguments.domains),
    )
    return scope, arguments.source_dir
