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
| [docs/PDR.md](docs/PDR.md) | Requirements F1 to F13, stack, phases, current status |
| [docs/architecture.md](docs/architecture.md) | Pipeline, components, workflow graph, data model, with diagrams |
| [docs/flows/](docs/flows/index.md) | What happens at runtime, one sequence diagram per flow, and known gaps |
| [CLAUDE.md](CLAUDE.md) | Hard rules, working agreement, API and configuration reference |
| [docs/fixtures-spec.md](docs/fixtures-spec.md) | The synthetic corpus and the gaps planted in it |
| [docs/plans/](docs/plans/) | The approved plan and outcome of each phase |

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Docker, for Postgres with pgvector and the optional Langfuse stack
- A model provider key for two steps only: drafting the plan and running the
  interview. Any provider LiteLLM supports.

## Set up

```bash
cp .env.example .env          # then change every password in it
docker compose up -d          # Postgres on 5432, Langfuse on http://localhost:3000
uv sync
uv run eki migrate
```

In `.env`, set the provider key (for example `ANTHROPIC_API_KEY`) and check
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

Steps 1, 2 and 5 need no model.

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

### The HTTP API

```bash
uv run uvicorn eki.api.app:app --reload     # http://localhost:8000/docs
```

Open `http://localhost:8000/` for the expert workspace. Sign in with an existing
API token to review plans, answer questions, review and approve cited entries,
search and export. Tokens stay in tab memory; after a refresh, sign in again
and reopen the saved conversation. Ingest and map the synthetic sources with
the CLI before drafting the first plan.

Every data route needs `Authorization: Bearer <token>`. The routes are listed
in [CLAUDE.md](CLAUDE.md) and their behaviour in [docs/flows/](docs/flows/index.md).

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
export ANTHROPIC_API_KEY=...
uv run pytest -m live -s -rs
```

## Status

Phases 0–4 and the Phase 5 code deliverables are implemented with synthetic
acceptance coverage: durable message recovery, an expert browser workspace,
model-aware artifact retrieval and resumable plan generation. Real-provider
validation is deferred by user request, and visual browser QA is pending because
browser control is unavailable. Source connectors remain synthetic. The
[Phase 5 PDR](docs/plans/phase-5.md) records evidence and remaining acceptance
work. Known gaps are listed in
[docs/flows/index.md](docs/flows/index.md).
