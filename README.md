<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
    <img src="docs/assets/logo.svg" alt="eki: Exit-Knowledge Interviewer. A question bubble on little legs chases an expert in a party hat, who sprints for an exit with a sunny palm tree beyond, dropping pages of knowledge on the way." width="330" height="80">
  </picture>
</p>

<p align="center"><em>Wait! One more question before you go.</em><br>
Because "it's all in Dave's head" is not a runbook.</p>

# eki: Exit-Knowledge Interviewer

Before an expert leaves, `eki` interviews them. It reads their tickets, code
history and selected emails, works out which topics only they know, asks about
those, and turns the answers into a searchable runbook in which every claim
cites where it came from. The expert approves the interview plan and every
runbook entry. After they hand over, a named successor keeps the entries
reviewed and current.

```text
Ingest -> Map gaps -> Plan -> Interview -> Synthesise -> Approve -> Searchable runbook
```

The workflow is a fixed LangGraph graph with one agentic node, the interviewer.

> **Synthetic data only.** Everything in this repository runs on the synthetic
> corpus in `tests/fixtures/corpus`. There is no connector to a live mailbox,
> Git host or issue tracker, and no run against a real model has been recorded
> yet (see [Status](#status)).

## Documentation

| Read | For |
| --- | --- |
| [docs/tutorial.md](docs/tutorial.md) | **Start here.** Your first run, step by step, with the output to expect |
| [docs/PDR.md](docs/PDR.md) | Requirements F1 to F22, stack, phases, current status |
| [docs/architecture.md](docs/architecture.md) | Pipeline, components, workflow graph, data model, with diagrams |
| [docs/flows/](docs/flows/index.md) | One page per runtime flow, recovery boundaries and known gaps |
| [docs/ui/ui-pdr.md](docs/ui/ui-pdr.md) | What the browser workspace must do: users, screens, states, acceptance criteria |
| [CLAUDE.md](CLAUDE.md) | Hard rules, working agreement, full API and configuration reference |
| [docs/fixtures-spec.md](docs/fixtures-spec.md) | The synthetic corpus and the gaps planted in it |
| [docs/plans/](docs/plans/) | The approved plan and outcome of each phase |
| [frontend/README.md](frontend/README.md) | Browser workspace source layout, development server and browser tests |
| [evals/README.md](evals/README.md) | Golden interview scenario formats, scoring and exit codes |

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Docker, for Postgres 16 with pgvector and the optional Langfuse stack
- A model provider key for plan drafting and interviewing. Everything else
  (ingest, mapping, review, search, export) runs without one.
- Node 26.13.0 (see `frontend/.nvmrc`), only for frontend development. The
  built workspace is committed, so running the API does not need Node.

## Set up

```bash
cp .env.example .env          # then change every password in it
docker compose up -d          # Postgres on 5432, Langfuse on http://localhost:3000
uv sync
uv run eki migrate            # also run after every pull that adds a migration
```

`docker compose up -d postgres` starts only the database if you do not want
tracing. The `eki` command reads `.env` itself; there is no need to export it.

## Configuration

All settings are environment variables read through `eki.config`;
[.env.example](.env.example) documents each one. The ones you are most likely
to change:

| Variable | Purpose |
| --- | --- |
| `EKI_DATABASE_URL` | Postgres connection string |
| `EKI_EXPERT_NAME`, `EKI_EXPERT_EMAIL` | The expert being interviewed (overridable per `eki ingest`) |
| `EKI_WORK_EMAIL_DOMAINS` | Work domains; other email addresses are redacted, and accounts use them unless `--allow-external-domain` is given |
| `EKI_SOURCE_DIR` | Corpus to ingest; defaults to `tests/fixtures/corpus` in the example |
| `EKI_MODEL_DRAFTER`, `EKI_MODEL_INTERVIEWER` | LiteLLM model names for plan drafting and interviewing (required for those steps) |
| `GROQ_API_KEY` (or your provider's variable) | Provider credential, under the provider's own name |
| `EKI_MODEL_TIMEOUT_SECONDS` | Per-call model timeout (default 60) |
| `EKI_MODEL_EMBEDDING` | Optional runbook embedder (256 dimensions). Blank uses offline hashing |
| `EKI_MODEL_ARTIFACT_EMBEDDING` | Optional source-artifact embedder (256 dimensions). Blank uses offline hashing |
| `EKI_MODEL_ANSWERER` | Optional Q&A evidence selector. Blank uses deterministic extraction |
| `EKI_SEARCH_MIN_SIMILARITY` | Vector match threshold (default 0.18); tune for your embedder |
| `EKI_PUBLIC_ORIGIN` | Exact browser origin, e.g. `http://localhost:8000`; HTTPS in production |
| `EKI_SESSION_IDLE_HOURS`, `EKI_SESSION_MAX_DAYS` | Browser session limits (8 hours idle, 7 days total) |
| `EKI_TRUSTED_PROXIES` | Proxy addresses, required behind a reverse proxy for sign-in throttling |
| `EKI_API_TOKENS` | Server registry of bearer identities (JSON array) |
| `EKI_API_TOKEN` | The CLI's own bearer token, for commands that act as an identity |
| `EKI_LANGFUSE_*` | Optional tracing; the audit log does not depend on it |

The hashing embedders exercise the vector pipeline but are lexical, not
semantic. Groq serves no embedding models: leave the embedding settings blank
or name another provider's model and add its key.

## Run the pipeline

Each command prints the ID the next one needs.

```bash
# 1. Read, redact and index the expert's work. Prints a run ID.
uv run eki ingest --since 2024-11-01 --until 2026-10-31
#    --mail-folder Inbox --mail-folder Sent   include selected mail folders only
#    --source-dir PATH                        read a different corpus

# 2. Group the work into topics and score the knowledge gaps.
uv run eki map  --run RUN_ID
uv run eki gaps --run RUN_ID --limit 10

# 3. Draft the interview plan (model calls). Prints a plan ID.
uv run eki plan --run RUN_ID
#    If interrupted, use the generation ID printed before drafting:
#    uv run eki plan --run RUN_ID --generation-status GENERATION_ID   (no model calls)
#    uv run eki plan --run RUN_ID --resume-generation GENERATION_ID   (finishes remaining topics)

# 4. Approve the plan and answer the questions (model calls).
#    /skip, /confidential ANSWER, /correct TURN TEXT, /quit to continue later.
uv run eki interview --plan PLAN_ID

# 5. Review the drafted runbook entries: approve, reject, edit or skip.
uv run eki entries --plan PLAN_ID
uv run eki review  --plan PLAN_ID
```

Mail is never read unless you name folders, and personal or private folders
are refused. If the interview is interrupted after an answer was accepted,
`eki interview` offers `/retry` on reconnect; the saved answer does not need
to be retyped. Entry review needs the reviewer's `EKI_API_TOKEN` (next section).

## Search, ask and export

These commands act as an API identity. Give the run an owner and register a
token for that owner:

```bash
uv run eki access --run RUN_ID --owner nadia
python -c 'import secrets; print(secrets.token_urlsafe(32))'   # make a token
```

```bash
# .env
EKI_API_TOKENS='[{"token":"THE_TOKEN","subject":"nadia","name":"Nadia Ferrand"}]'
EKI_API_TOKEN=THE_TOKEN
```

```bash
uv run eki search "month-end reconciliation"        # add --json for machine output
uv run eki ask "How do I roll back reconciliation?"
uv run eki export --run RUN_ID --output runbook.md  # refuses to overwrite a file
```

Only approved entries are found. `eki ask` answers with exact approved claims
and their citations, or says the runbook does not cover the question.

Approving an entry queues a search-index job. The API processes these in the
background. Without the API running, process them with
`uv run eki index-runbook --once`, or leave `uv run eki index-runbook` running
for automatic retries. Entries can be opened and exported while they are being
indexed. See the [search and indexing flow](docs/flows/search-and-ask.md).

## Browser workspace and HTTP API

```bash
uv run uvicorn eki.api.app:app --reload     # workspace at http://localhost:8000/, API docs at /docs
```

Create an invited account and give it a run:

```bash
uv run eki users add --email nadia.ferrand@example.com --name "Nadia Ferrand" --subject nadia
uv run eki access --run RUN_ID --owner nadia
```

Open the printed invitation link (valid for 72 hours), set a password, then
sign in at `http://localhost:8000/`. No email is sent; operators hand links
over. Ingest and map with the CLI before drafting the first plan in the
browser. The [browser walkthrough](docs/tutorial.md#optional-the-browser-workspace)
covers the full workflow and the [sign-in flow](docs/flows/sign-in.md) covers
password resets, session revocation and proxy configuration.

Data routes accept either a session cookie or `Authorization: Bearer <token>`.
Cookie-authenticated changes must come from `EKI_PUBLIC_ORIGIN`. Routes are
listed in [CLAUDE.md](CLAUDE.md) and their behaviour in [docs/flows/](docs/flows/index.md).

## After the expert leaves: maintenance

An administrator transfers a run to an active successor in the **Maintenance**
tab or with `eki handover`. Interview capture then closes. The successor edits
and re-approves entries under their own identity; original answers and
approvals keep their attribution.

Approved entries have review-by dates (90 days by default) and show due-soon
and overdue labels. Overdue entries stay readable, searchable and exportable,
but Ask no longer uses them. Readers can flag an entry privately; flags never
change publication or renew a date by themselves.

```bash
uv run eki maintenance --run RUN_ID --filter overdue   # shows owner and run revision
uv run eki handover --run RUN_ID --to samir --expected-owner nadia \
  --expected-run-revision REVISION --reason "Expert departure"
uv run eki review --run RUN_ID --due
```

Reassigning an owned run with `eki access --owner` or `PUT /runs/{id}/owner`
also needs the expected owner and run revision. See the
[maintenance guide](docs/flows/maintenance.md).

## Follow-up interviews

While the original expert is still reachable, the **Follow-ups** tab revisits
open sections or reader questions that Ask could not answer ("Save for a
follow-up"). The owner triages the questions and reviews a focused plan with
call limits. The expert approves and answers it. Answers become private
proposals: apply the chosen sections, then approve the entry again to publish.

```bash
uv run eki follow-up gaps --run RUN_ID
uv run eki follow-up plan --run RUN_ID --parent-session FINISHED_SESSION_ID --gap GAP_ID
uv run eki interview --plan FOLLOW_UP_PLAN_ID
uv run eki follow-up proposals --session FOLLOW_UP_SESSION_ID
uv run eki follow-up apply --proposal PROPOSAL_ID --expected-revision REVISION \
  --expected-version ENTRY_VERSION --section rollback=merge
uv run eki review --plan ORIGINAL_PLAN_ID
uv run eki follow-up resolve --run RUN_ID --gap REQUEST_GAP_ID --entry ENTRY_ID \
  --expected-revision GAP_REVISION --expected-version APPROVED_VERSION --confirms-answer
```

These commands need `EKI_API_TOKEN`. Only the interview step calls a model.
`eki follow-up --help` lists triage, plan editing, discard, refresh and stop.
See the [follow-up guide](docs/flows/follow-up.md).

## Operator commands

These use database credentials directly and are meant for a trusted local operator.

| Command | Does |
| --- | --- |
| `eki users add / update / reset-password / disable / enable / revoke-sessions / list` | Manage browser accounts; reset links last one hour |
| `eki access --run ID --owner SUBJECT --readers SUBJECT...` | Set run owner and readers (`--readers` with no subjects revokes all) |
| `eki audit --run ID` / `eki audit --auth` | Read the metadata-only audit log of a run, or of sign-ins |
| `eki index-runbook [--once]` | Process queued search-index jobs |
| `eki reindex-artifacts --run ID` | Replace a run's artifact index with the configured artifact embedder |

## Upgrading an existing database

Run `uv run eki migrate` before starting a newer API. Some migrations have side
effects worth knowing:

- **0013–0015** backfill review dates as approval time plus 90 days, so older
  entries may be overdue straight away. Review that queue after migrating.
- **0019** queues search-index jobs for approved entries. Until they are
  processed, Ask may abstain with reason `indexing`.

Existing runs keep the artifact embedder they were ingested with until you run
`eki reindex-artifacts`.

## Development

### Checks

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest -q                                        # fast tests only (excludes db, infra, live)
uv run pytest -q -m db                                  # database tests; needs Postgres up
uv run python evals/run.py --junit reports/quality.xml  # quality and red-team gate
```

```bash
cd frontend
npm ci
npm run typecheck
npm run format:check
npm test              # delivery/SSE logic (Vitest)
npm run test:e2e      # browser workflows and accessibility (Playwright)
```

More than half of the Python tests are database tests, so a plain
`uv run pytest` skips them. CI runs all of the above on every push and pull
request. It also rebuilds the frontend and fails if the committed assets
differ, and it runs the follow-up journey in a real browser against the API
(`uv run pytest -q -m db tests/test_follow_up_browser.py`). CI never calls a
paid model.

For frontend work, run `npm run dev` in `frontend/` alongside the API; see
[frontend/README.md](frontend/README.md) for the origin and cookie settings.
Regenerate the synthetic corpus with `uv run python -m tests.fixtures.generator`.

### Without a provider key

The whole pipeline is exercised by tests that use scripted model doubles and a
simulated expert:

```bash
uv run pytest -m db -v tests/test_phase2_acceptance.py tests/test_phase3a_acceptance.py
```

These prove the workflow, storage, review and privacy rules. They say nothing
about how well a real model interviews.

### Real-model tests

Tests that call a real provider are opt-in and cost money. Set the model names,
then export the provider key to the shell. A key that is only in `.env` does
not count as consent:

```bash
export GROQ_API_KEY=...
uv run pytest -m live -s -rs
```

### Golden interview evaluations

`evals/grade.py` scores real-model interview quality against scenario and
golden files. It is separate from CI. Validate the eight review drafts without
a database or model:

```bash
uv run python evals/grade.py --dry-run \
  --scenarios docs/plans/phase-7-drafts/scenarios \
  --golden docs/plans/phase-7-drafts/golden
```

To grade them, configure `EKI_DATABASE_URL`, `EKI_MODEL_DRAFTER` and
`EKI_MODEL_INTERVIEWER`, export the provider key, then remove `--dry-run` and
optionally add `--only G3-month-end --max-calls 25`. Each scenario runs in a
temporary Postgres schema within a shared call budget. The command prints a
scorecard and saves a redacted JSON report under `reports/evals/`; add
`--compare PATH` to compare with an earlier report.

`evals/scenarios/` and `evals/golden/` are human-owned and still empty; the
owner reviews and adopts the drafts. Details are in [evals/README.md](evals/README.md).

## Project layout

```text
src/eki/
  api/          FastAPI app and routes by area
  cli/          the `eki` command (follow-up commands: followup_cli.py)
  graph/        LangGraph workflow around the one agentic node
  ingest/       connector clients, redaction, artifact indexing
  mapping/      topic clustering and gap scoring
  interview/    plan drafting, the interviewer and its tools, sessions
  runbook/      entry template, synthesis, citations, review
  search/       approved-only hybrid search and cited Q&A
  followup/     follow-up gaps, sessions and proposals
  llm/          model interface, LiteLLM implementation, tracing
  web/static/   built browser workspace (source in frontend/)
  migrations/   SQL migrations applied by `eki migrate`
mcp_servers/    read-only git, tickets and mail servers
frontend/       React + TypeScript workspace source
evals/          quality gate runner and golden interview grader
tests/          tests; synthetic corpus in tests/fixtures/
```

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 0–4 | Scaffold, ingest and mapping, interview, runbook, hardening (mail, audit, red-team, CI gate) | Implemented |
| 5 | Durable message delivery, browser workspace, semantic artifact retrieval, resumable plan drafting | Implemented; real-model evidence deferred, manual screen-reader check pending |
| 6 | Email/password accounts and browser sessions | Implemented; manual browser pass pending |
| 7 | Golden interview evaluations | Implemented; drafts await owner adoption, no paid run yet |
| 8 | Successor handover, review dates, reader flags | Implemented |
| 9 | Focused follow-up interviews | Implemented with synthetic acceptance evidence |
| 10 | Configurable model providers per role | [Proposed](docs/plans/phase-10.md), awaiting approval |

Still open across phases:

- No run against a real model yet, so interview quality, semantic retrieval
  and model-selected answers are unverified.
- Source connectors read the synthetic corpus only.
- A model call interrupted before its result was saved may run, and be billed,
  again on recovery.

The full list of known gaps is in [docs/flows/index.md](docs/flows/index.md).
