"""``eki search``, ``ask`` and ``export``, authenticated with ``EKI_API_TOKEN``."""

import argparse
import asyncio
import json
from contextlib import suppress

from eki.auth import AuthenticationError, TokenAuthenticator
from eki.cli.common import fail
from eki.config import Settings, work_domains
from eki.runbook.export import ExportService
from eki.search.answers import build_answers
from eki.search.jobs import IndexWorker
from eki.search.service import build_search


def index_jobs(arguments: argparse.Namespace, settings: Settings) -> int:
    if not 1 <= arguments.limit <= 10000:
        raise ValueError("limit must be between 1 and 10000")
    search = build_search(settings)
    worker = IndexWorker(search.dsn, search.embedder, search.work_email_domains)
    if arguments.once:
        counts = worker.drain(limit=arguments.limit)
        print(json.dumps(counts))
        return 1 if counts["retrying"] else 0
    with suppress(KeyboardInterrupt):
        asyncio.run(worker.serve(asyncio.Event()))
    return 0


def execute(arguments: argparse.Namespace, settings: Settings) -> int:
    credential = settings.api_token
    if credential is None or not credential.get_secret_value():
        raise AuthenticationError("set EKI_API_TOKEN to a configured client credential")
    principal = TokenAuthenticator(settings.api_tokens.get_secret_value()).authenticate(
        "Bearer " + credential.get_secret_value()
    )
    if arguments.command == "export":
        exports = ExportService(settings.database_url.get_secret_value(), work_domains(settings))
        content = (
            exports.entry(principal, arguments.entry)
            if arguments.entry
            else exports.run(principal, arguments.run)
        )
        if arguments.output is None:
            print(content, end="")
        else:
            try:
                with arguments.output.open("x", encoding="utf-8") as output:
                    output.write(content)
            except OSError:
                return fail(
                    "cannot create output file; check permissions and that it does not exist"
                )
            print(f"Exported to {arguments.output}")
        return 0
    search = build_search(settings)
    if arguments.command == "ask":
        answer = asyncio.run(build_answers(settings, search).ask(principal, arguments.question))
        print(answer.model_dump_json() if arguments.json else answer.answer)
        if not arguments.json:
            for evidence in answer.evidence:
                print(
                    f"[{evidence.id}] {evidence.title}, {evidence.section}, "
                    f"version {evidence.version}"
                )
        return 0
    hits = search.search(principal, arguments.query, limit=arguments.limit)
    if arguments.json:
        print(
            json.dumps(
                {
                    "results": [h.public() for h in hits],
                    "embedding_model": search.embedder.name,
                    "indexing": search.indexing(principal),
                }
            )
        )
    else:
        for hit in hits:
            print(f"{hit.entry.title} [{hit.entry.id}] ({hit.section})\n  {hit.snippet}")
        if not hits:
            print(
                "Approved entries are still being prepared for search; "
                "run eki index-runbook --once "
                "or keep the API worker running."
                if search.indexing(principal)["pending_count"]
                else "No matching approved entries."
            )
    return 0
