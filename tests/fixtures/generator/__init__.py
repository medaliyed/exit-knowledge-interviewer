"""Deterministic generator for the synthetic corpus in docs/fixtures-spec.md.

Usage::

    uv run python -m tests.fixtures.generator [--seed N] [--out DIR]

The same seed always produces the same bytes. Everything generated is
invented; see ``world.py``.
"""

import random
from pathlib import Path

from tests.fixtures.generator.answers import expert_answers
from tests.fixtures.generator.filler import fill_everything
from tests.fixtures.generator.plants import plant_everything
from tests.fixtures.generator.world import EXPERT, Corpus
from tests.fixtures.generator.write import RefusingToOverwriteError, write_fixtures

DEFAULT_SEED = 1207
DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent

__all__ = ["DEFAULT_OUT_DIR", "DEFAULT_SEED", "RefusingToOverwriteError", "build", "generate"]


def build(seed: int) -> Corpus:
    """Build the corpus in memory: plants first, then filler, then stable ordering and IDs."""
    rng = random.Random(seed)  # noqa: S311 - reproducible test data, not cryptography
    corpus = Corpus()
    plant_everything(rng, corpus)
    fill_everything(rng, corpus)

    corpus.tickets.sort(key=lambda ticket: ticket.created_at)
    corpus.emails.sort(key=lambda email: email.sent_at)
    corpus.pull_requests.sort(key=lambda pull_request: pull_request.created_at)
    corpus.commits.sort(key=lambda commit: (commit.authored_at, commit.sha))
    corpus.wiki.sort(key=lambda page: page.slug)
    for number, ticket in enumerate(corpus.tickets, start=1):
        ticket.id = f"TCK-{number:04d}"
    for number, email in enumerate(corpus.emails, start=1):
        email.id = f"EML-{number:04d}"
    for number, pull_request in enumerate(corpus.pull_requests, start=1):
        pull_request.id = f"PR-{number:03d}"
    return corpus


def generate(seed: int = DEFAULT_SEED, out_dir: Path = DEFAULT_OUT_DIR) -> None:
    """Write ``corpus/``, ``answer_key.json`` and ``expert_answers.json`` under ``out_dir``."""
    write_fixtures(out_dir, build(seed), seed, expert_answers(EXPERT.name))
