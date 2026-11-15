# eki: Exit-Knowledge Interviewer

Before an expert leaves, `eki` interviews them. It reads their tickets, code
history and selected emails, works out which topics only they know, asks about
those, and turns the answers into a searchable runbook in which every claim
cites where it came from. The expert approves the interview plan and every
runbook entry.

Everything in this repository runs on a synthetic corpus. There is no
connector to a live mailbox, Git host or issue tracker.

## Documentation

| Read | For |
| --- | --- |
| [docs/tutorial.md](docs/tutorial.md) | **Start here.** Your first run, step by step, with the output to expect |
| [docs/ui/ui-pdr.md](docs/ui/ui-pdr.md) | What the browser workspace must do: users, screens, states, constraints, acceptance criteria |
| [docs/PDR.md](docs/PDR.md) | Requirements F1 to F19, stack, phases, current status |
| [docs/architecture.md](docs/architecture.md) | Pipeline, components, workflow graph, data model, with diagrams |
| [Browser workspace](docs/flows/browser-workspace.md) | Sign in, review plans, interview, recover saved work and approve entries |
| [docs/flows/](docs/flows/index.md) | Runtime flows, recovery boundaries and known gaps |
| [CLAUDE.md](CLAUDE.md) | Hard rules, working agreement, API and configuration reference |
| [docs/fixtures-spec.md](docs/fixtures-spec.md) | The synthetic corpus and the gaps planted in it |
| [docs/plans/](docs/plans/) | The approved plan and outcome of each phase |

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Docker, for Postgres with pgvector and the optional Langfuse stack
- A configured model/provider key for drafting and interviewing. Optional
  semantic embeddings and Q&A evidence selection also use a provider when enabled.

## Set up

```bash
cp .env.example .env          # then change every password in it
docker compose up -d          # Postgres on 5432, Langfuse on http://localhost:3000
uv sync
uv run eki migrate
```

In `.env`, set the provider key (for example `GROQ_API_KEY`) and check
`EKI_MODEL_DRAFTER` and `EKI_MODEL_INTERVIEWER`. The `eki` command reads `.env`
by itself; there is no need to export it.

## Run the pipeline

Each command prints the ID the next one needs.

```bash
# 1. Read, redact and index the expert's work. Prints a run ID.
uv run eki ingest --since 2024-11-01 --until 2026-10-31
#    add --mail-folder Inbox --mail-folder Sent to include selected mail

# 2. Group the work into topics and score the knowledge gaps.
uv run eki map  --run RUN_ID
uv run eki gaps --run RUN_ID --limit 10

# 3. Draft the interview plan (model call). Prints a plan ID.
uv run eki plan --run RUN_ID
# If interrupted, use the generation ID printed before drafting:
# uv run eki plan --run RUN_ID --generation-status GENERATION_ID
# uv run eki plan --run RUN_ID --resume-generation GENERATION_ID

# 4. Approve the plan and answer the questions (model calls).
#    /skip, /confidential ANSWER, /correct TURN TEXT, /quit to continue later.
uv run eki interview --plan PLAN_ID

# 5. Review the drafted runbook entries: approve, reject, edit or skip.
uv run eki entries --plan PLAN_ID
uv run eki review  --plan PLAN_ID
```

With the optional artifact embedder blank, steps 1, 2 and 5 need no provider.
Mapping and entry review always run locally. The CLI offers `/retry` if an
accepted interview message needs processing to resume; the saved answer does
not need to be retyped.

### Search, ask and export

These act as an API identity, so they need an owner and a token:

```bash
uv run eki access --run RUN_ID --owner nadia
python -c 'import secrets; print(secrets.token_urlsafe(32))'   # make a token
```

Put the token in `.env`:

```bash
EKI_API_TOKENS='[{"token":"THE_TOKEN","subject":"nadia","name":"Nadia Ferrand"}]'
EKI_API_TOKEN=THE_TOKEN
```

```bash
uv run eki search "month-end reconciliation"
uv run eki ask "How do I roll back reconciliation?"
uv run eki export --run RUN_ID --output runbook.md
```

Only approved entries are found. `eki ask` answers with exact approved claims
and their citations, or says the runbook does not cover the question.

### Artifact retrieval

Blank `EKI_MODEL_ARTIFACT_EMBEDDING` uses offline hashing. Set it to a LiteLLM
embedding model supporting 256 dimensions for optional semantic artifact
retrieval. Existing runs keep their saved model; use
`uv run eki reindex-artifacts --run RUN_ID` to replace that index explicitly.
`EKI_MODEL_EMBEDDING` controls runbook retrieval separately.

### Browser workspace and HTTP API

```bash
uv run uvicorn eki.api.app:app --reload     # http://localhost:8000/docs
```

Set `EKI_PUBLIC_ORIGIN=http://localhost:8000` in `.env` (use your HTTPS origin
in production), apply migrations, and create an invited account:

```bash
uv run eki users add --email nadia.ferrand@example.com --name "Nadia Ferrand" --subject nadia
uv run eki access --run RUN_ID --owner nadia
```

Open the printed invitation link, set a password, then use email and password
at `http://localhost:8000/`. The server keeps a bounded session in an HttpOnly
cookie, so reloading reopens saved work at the current URL. Unsent answers remain
in tab memory. Ingest/map the synthetic sources with the CLI before drafting
the first plan. The [sign-in flow](docs/flows/sign-in.md) covers invitations,
password resets, session revocation and configuration; the
[browser walkthrough](docs/tutorial.md#optional-the-browser-workspace) covers
the full workflow. Accounts do not send email; operators hand over printed links.

The redesigned workspace follows the HTML prototype and UI PDR with React,
TypeScript and Vite. Its production assets are included, so running the API is
enough to use it. For frontend development, run `npm ci` and `npm run dev` in
`frontend/`; see [frontend/README.md](frontend/README.md) for build and browser tests.

Data routes accept a browser session cookie or `Authorization: Bearer <token>`.
Bearer tokens continue to serve the CLI and automation. Cookie-authenticated
mutations require an `Origin` matching `EKI_PUBLIC_ORIGIN`. The routes are listed
in [CLAUDE.md](CLAUDE.md) and their behaviour in [docs/flows/](docs/flows/index.md).

## Maintaining entries after handover

Phase 8 keeps knowledge reviewable after the original expert leaves. Apply
`uv run eki migrate` before starting the updated API. An administrator can use
the **Maintenance** tab to transfer responsibility to an active successor; the
successor can edit and re-approve entries under their own identity. Original
answers and approvals retain their attribution.

Entries have review-by dates, due-soon/overdue labels and private reader reports.
Unchanged review renews the date without rewriting content. Overdue approved
entries remain readable, searchable and exportable, while Ask excludes them.
A reader report does not automatically change publication or renew a date.

```bash
uv run eki maintenance --run RUN_ID --filter overdue
uv run eki handover --run RUN_ID --to samir --expected-owner nadia \
  --expected-run-revision REVISION --reason "Expert departure"
uv run eki review --run RUN_ID --due
```

Reassigning an already-owned run with `eki access --owner` or `PUT /runs/{id}/owner`
also needs `--expected-owner` and `--expected-run-revision` (or the matching JSON
fields); a first assignment does not.
The maintenance command shows the current run revision. Provision/register the
successor before transfer. Review mutations in the CLI now require that
reviewer's `EKI_API_TOKEN`; local operator inspection and account/ownership
commands retain their existing trust model. See the
[maintenance guide](docs/flows/maintenance.md) for retries, flags and rollout.
Legacy review dates are backfilled from approval time plus 90 days, so some
older entries become overdue as soon as the migration is applied.

## Without a provider key

The whole pipeline is exercised by tests that use scripted model doubles and a
simulated expert:

```bash
uv run pytest -m db -v tests/test_phase2_acceptance.py tests/test_phase3a_acceptance.py
```

These prove the workflow, storage, review and privacy rules. They say nothing
about how well a real model interviews.

## Checks

```bash
uv run pytest -q                                        # unit tests
uv run pytest -q -m db                                  # database tests; needs Postgres up
uv run python evals/run.py --junit reports/quality.xml  # quality and red-team gate
uv run ruff check . && uv run ruff format --check .
uv run mypy
node --test tests/browser_client.test.cjs  # client delivery/SSE logic; optional Node dev check
```

Tests that call a real provider are opt-in and cost money. They need the model
names set and the provider key exported to the shell, not only present in
`.env`:

```bash
export GROQ_API_KEY=...
uv run pytest -m live -s -rs
```

## Golden interview evaluations

Phase 7 adds a scorecard for real-model interview quality. Start with the eight
review drafts; validation performs no database or provider work:

```bash
uv run python evals/grade.py --dry-run \
  --scenarios docs/plans/phase-7-drafts/scenarios \
  --golden docs/plans/phase-7-drafts/golden
```

To grade them, configure `EKI_DATABASE_URL`, `EKI_MODEL_DRAFTER` and
`EKI_MODEL_INTERVIEWER`, export the provider key, then remove `--dry-run` and
optionally add `--only G3-month-end --max-calls 25`. Each scenario uses a temporary
Postgres schema and a shared call budget. The command prints a scorecard and
saves a redacted JSON report under `reports/evals/`. Add `--compare PATH` to
compare with an earlier report. This is separate from the CI quality gate.

The default `evals/scenarios/` and `evals/golden/` directories remain human-owned
and unpopulated. The owner reviews and adopts the drafts; the grader only reads
them. Formats, scoring limits and exit codes are in [evals/README.md](evals/README.md).

## Status

Phases 0–4 and the Phase 5 code deliverables are implemented with synthetic
acceptance coverage: durable message recovery, an expert browser workspace,
model-aware artifact retrieval and resumable plan generation. The redesigned
workspace follows the supplied HTML prototype and UI PDR, with automated browser
workflow, accessibility and responsive-layout checks. Phase 6 adds invited
email/password accounts, persistent browser sessions and operator account management.
Phase 8 adds successor handover, review dates and private reader-feedback triage.
Phase 7 adds golden interview grading, bounded runs and report comparison, with
eight scenario/golden drafts awaiting owner adoption. Real-provider validation
remains deferred by user request; manual screen-reader validation is still pending.
Source connectors remain synthetic. The
[Phase 5 PDR](docs/plans/phase-5.md) records evidence and remaining acceptance
work. Known gaps are listed in
[docs/flows/index.md](docs/flows/index.md).
