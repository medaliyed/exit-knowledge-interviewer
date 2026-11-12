"""Background artifacts that bring the corpus up to the exact sizes in the spec.

Filler is deliberately bland: ordinary work by all five people across the four
main systems. It must never repeat a planted phrase, touch report-export or
notify-relay commits (those repos carry G2 and N2), or write to the vendor.
"""

import random
from datetime import timedelta

from tests.fixtures.generator.world import (
    COLLEAGUES,
    ENGINEERS,
    EXPERT,
    SPAN_END,
    SYSTEMS,
    TEAM,
    ChangedFile,
    Comment,
    Commit,
    Corpus,
    Email,
    Person,
    PullRequest,
    Review,
    Ticket,
    clamp,
    new_sha,
    random_moment,
)

TICKET_COUNT = 120
COMMIT_COUNT = 300
PULL_REQUEST_COUNT = 40
EMAIL_COUNT = 80

# The expert carries more of the load than anyone else, as in the scenario.
ASSIGNEES: tuple[Person, ...] = (EXPERT, EXPERT, EXPERT, *ENGINEERS[1:])
AUTHORS: tuple[Person, ...] = (EXPERT, EXPERT, *ENGINEERS[1:])
SHARED_REPOS = ("billing-batch", "ledger-db", "gateway")

TICKET_TITLES: dict[str, tuple[str, ...]] = {
    "billing-batch": (
        "Posting job slower than usual on the Tuesday run",
        "Add a retry metric to the settlement step",
        "Batch worker pod evicted during the run",
        "Clarify invoice rounding for multi-currency tenants",
        "Upgrade the batch base image",
    ),
    "ledger-db": (
        "Replication lag alert flapping on replica-2",
        "Add an index for the period lookup",
        "Vacuum is falling behind on the postings table",
        "Connection pool exhausted during a load test",
        "Rotate the read-only service account",
    ),
    "gateway": (
        "Intermittent 502 from the canary pool",
        "Add a request-id header to upstream calls",
        "Tighten the idle timeout for partner routes",
        "Dashboard shows a wrong p99 for the admin API",
        "Access log sampling is too aggressive",
    ),
    "report-export": (
        "Regulator export arrived late",
        "Column widths are wrong in the settlement sheet",
        "Export for the previous period is empty",
        "Add a regional breakdown to the tax export",
        "Export job needs more memory at quarter close",
    ),
}
TICKET_COMMENTS = (
    "Reproduced on staging.",
    "Fix deployed, watching it for a day.",
    "Config had drifted between pools; aligned them.",
    "No recurrence in a week, closing.",
    "Added a dashboard panel for this.",
    "Rolled the change back and re-applied it after review.",
    "Linked the pull request.",
    "Needs a maintenance window; scheduled for Thursday.",
)
COMMIT_SUBJECTS: dict[str, tuple[str, ...]] = {
    "billing-batch": (
        "fix(posting): retry on a transient ledger timeout",
        "feat(settlement): emit a per-tenant duration metric",
        "refactor(worker): split fetch and post stages",
        "test(posting): cover the multi-currency rounding cases",
        "docs: describe the worker environment variables",
        "fix(worker): release the lease on shutdown",
    ),
    "ledger-db": (
        "feat(schema): add an index on the posting period",
        "fix(migrations): make migration 0042 idempotent",
        "ops: tune autovacuum for the postings table",
        "test: add a replication lag smoke test",
        "docs: document the read-only role",
        "fix(pool): cap the connections per service",
    ),
    "gateway": (
        "fix(proxy): pass the request-id header upstream",
        "feat(routes): add an idle timeout per route",
        "refactor(config): load pools from a single file",
        "test(routes): cover the partner tier routing",
        "docs: explain access log sampling",
        "fix(metrics): correct the p99 bucket boundaries",
    ),
}
COMMIT_PATHS: dict[str, tuple[str, ...]] = {
    "billing-batch": ("posting/job.py", "worker/main.py", "settlement/metrics.py", "README.md"),
    "ledger-db": ("migrations/0042.sql", "ops/autovacuum.conf", "roles/readonly.sql", "README.md"),
    "gateway": ("proxy/headers.go", "routes/partner.yaml", "metrics/buckets.go", "README.md"),
}
REVIEW_BODIES = (
    "Looks good.",
    "Thanks for adding the tests.",
    "One nit on naming, otherwise fine.",
    "Approved after the follow-up commit.",
)
EMAIL_TOPICS = (
    (
        "Capacity review for {system}",
        "Can we go through the {system} capacity numbers on Thursday?",
    ),
    (
        "Post-incident notes for {system}",
        "Notes from the {system} review are in the shared folder.",
    ),
    ("Planning: {system} next quarter", "Please add your {system} items to the planning sheet."),
    ("Maintenance window for {system}", "The {system} window is confirmed for Sunday 06:00 UTC."),
    ("Question about a {system} alert", "Is the new {system} alert threshold intended?"),
    ("Handover for the {system} on-call week", "Nothing open on {system}. Quiet week."),
)


def fill_tickets(rng: random.Random, corpus: Corpus) -> None:
    quiet_period = SPAN_END - timedelta(days=10)
    while len(corpus.tickets) < TICKET_COUNT:
        system = rng.choice(SYSTEMS)
        created = random_moment(rng)
        reporter = rng.choice(COLLEAGUES)
        assignee = rng.choice(ASSIGNEES)
        comments = []
        moment = created
        still_open = created > quiet_period
        for _ in range(1 if still_open else rng.randint(1, 3)):
            moment = clamp(moment + timedelta(minutes=rng.randint(20, 900)))
            author = rng.choice((assignee, assignee, reporter))
            comments.append(Comment(author, moment, rng.choice(TICKET_COMMENTS)))
        resolved = None if still_open else clamp(moment + timedelta(minutes=rng.randint(5, 240)))
        corpus.tickets.append(
            Ticket(
                system=system,
                type=rng.choice(("incident", "request", "task")),
                title=rng.choice(TICKET_TITLES[system]),
                priority=rng.choice(("P2", "P3", "P3", "P4")),
                reporter=reporter,
                assignee=assignee,
                created_at=created,
                resolved_at=resolved,
                description=f"Raised by {reporter.name} for {system}. Details in the comments.",
                comments=comments,
                labels=[system],
            )
        )


def fill_commits(rng: random.Random, corpus: Corpus) -> list[Commit]:
    added = []
    while len(corpus.commits) < COMMIT_COUNT:
        repo = rng.choice(SHARED_REPOS)
        commit = Commit(
            sha=new_sha(rng),
            repo=repo,
            author=rng.choice(AUTHORS),
            authored_at=random_moment(rng),
            message=rng.choice(COMMIT_SUBJECTS[repo]),
            files=[
                ChangedFile(path, rng.randint(1, 80), rng.randint(0, 30))
                for path in rng.sample(COMMIT_PATHS[repo], rng.randint(1, 2))
            ],
        )
        corpus.commits.append(commit)
        added.append(commit)
    return added


def fill_pull_requests(rng: random.Random, corpus: Corpus, candidates: list[Commit]) -> None:
    """Wrap runs of one author's filler commits in one repo into reviewed pull requests."""
    groups: dict[tuple[str, str], list[Commit]] = {}
    for commit in sorted(candidates, key=lambda c: (c.authored_at, c.sha)):
        groups.setdefault((commit.repo, commit.author.email), []).append(commit)
    chunks: list[list[Commit]] = []
    for key in sorted(groups):
        remaining = groups[key]
        while remaining:
            size = rng.randint(1, 3)
            chunks.append(remaining[:size])
            remaining = remaining[size:]
    needed = PULL_REQUEST_COUNT - len(corpus.pull_requests)
    for number, index in enumerate(sorted(rng.sample(range(len(chunks)), needed))):
        commits = chunks[index]
        author = commits[0].author
        created = clamp(commits[-1].authored_at + timedelta(minutes=rng.randint(10, 90)))
        superseded = number % 10 == 9
        reviewers = rng.sample([p for p in ENGINEERS if p != author], rng.randint(1, 2))
        reviews = []
        moment = created
        for reviewer in reviewers:
            moment = clamp(moment + timedelta(minutes=rng.randint(30, 600)))
            if superseded:
                reviews.append(Review(reviewer, "commented", moment, "Superseded by a follow-up."))
            else:
                reviews.append(Review(reviewer, "approved", moment, rng.choice(REVIEW_BODIES)))
        pull_request = PullRequest(
            repo=commits[0].repo,
            title=commits[0].message.split(": ", 1)[-1].capitalize(),
            author=author,
            state="closed" if superseded else "merged",
            created_at=created,
            closed_at=clamp(moment + timedelta(minutes=rng.randint(5, 60))),
            description=f"{len(commits)} commit(s). See the commit messages for detail.",
            commits=commits,
            reviews=reviews,
        )
        for commit in commits:
            commit.pull_request = pull_request
        corpus.pull_requests.append(pull_request)


def fill_emails(rng: random.Random, corpus: Corpus) -> None:
    index = 0
    while len(corpus.emails) < EMAIL_COUNT:
        sender = rng.choice(TEAM)
        system = rng.choice(SYSTEMS)
        subject, body = rng.choice(EMAIL_TOPICS)
        if sender == EXPERT:
            folder, recipient = "Sent", rng.choice(COLLEAGUES)
        else:
            folder, recipient = "Inbox", EXPERT
        first_name = sender.name.split()[0]
        corpus.emails.append(
            Email(
                folder=folder,
                sensitivity="normal",
                sender=sender.email,
                to=[recipient.email],
                sent_at=random_moment(rng),
                subject=subject.format(system=system),
                body=f"{body.format(system=system)}\n\n{first_name}",
                thread=f"filler-{index}",
            )
        )
        index += 1


def fill_everything(rng: random.Random, corpus: Corpus) -> None:
    fill_tickets(rng, corpus)
    filler_commits = fill_commits(rng, corpus)
    fill_pull_requests(rng, corpus, filler_commits)
    fill_emails(rng, corpus)
