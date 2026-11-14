"""Shared pytest configuration and fixtures."""

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

import psycopg
import pytest
from mcp import Client
from psycopg import sql
from psycopg.conninfo import make_conninfo

from eki.config import get_settings
from eki.db import connect, migrate
from eki.graph.workflow import setup_checkpoints
from eki.ingest.models import Snapshot
from eki.ingest.pipeline import pull
from mcp_servers.git_server import build_server as build_git
from mcp_servers.tickets_server import build_server as build_tickets
from tests.support import CORPUS, FULL_SPAN

# LiteLLM loads the local .env file when imported unless told it is in production.
# Tests must not depend on a developer's .env, and must never download its price list.
os.environ.setdefault("LITELLM_MODE", "PRODUCTION")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


def _scratch_connection(*, migrated: bool) -> Iterator[psycopg.Connection]:
    """A connection whose objects live in a throwaway schema, so tests never touch real data."""
    schema = f"test_{uuid.uuid4().hex[:12]}"
    connection = connect(get_settings())
    connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    connection.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
    connection.commit()
    if migrated:
        migrate(connection)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        connection.commit()
        connection.close()


@dataclass(frozen=True)
class Scratch:
    """A migrated throwaway schema that any number of connections can share."""

    schema: str
    dsn: str = field(repr=False)
    """A connection string whose search path is the scratch schema."""

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, autocommit=True)


@pytest.fixture
def scratch() -> Iterator[Scratch]:
    """Committed scratch schema with every migration and the checkpoint tables."""
    schema = f"test_{uuid.uuid4().hex[:12]}"
    base = get_settings().database_url.get_secret_value()
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    dsn = make_conninfo(base, options=f"-c search_path={schema},public")
    try:
        with psycopg.connect(dsn) as connection:
            migrate(connection)
        setup_checkpoints(dsn)
        yield Scratch(schema=schema, dsn=dsn)
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def empty_conn() -> Iterator[psycopg.Connection]:
    """A scratch schema with no tables."""
    yield from _scratch_connection(migrated=False)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    """A scratch schema with every migration applied."""
    yield from _scratch_connection(migrated=True)


@pytest.fixture
async def snapshot() -> Snapshot:
    """The fixture corpus, pulled through the real MCP servers."""
    async with (
        Client(build_git(FULL_SPAN, CORPUS), raise_exceptions=True) as git,
        Client(build_tickets(FULL_SPAN, CORPUS), raise_exceptions=True) as tickets,
    ):
        return await pull(git, tickets, FULL_SPAN)
