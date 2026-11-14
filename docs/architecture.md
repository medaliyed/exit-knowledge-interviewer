# Architecture

How `eki` is put together: the pipeline, the components, the interview
workflow, the data model and the rules that hold across all of them. For what
happens step by step at runtime, see the flow pages in [flows/](flows/index.md).

All diagrams are Mermaid. GitHub renders them; in VS Code install a Mermaid
preview extension.

## The pipeline

Seven stages turn an expert's work history into a searchable runbook. A model
is used in two of them. People decide twice: the expert approves the plan, and
the expert approves every entry.

```mermaid
flowchart LR
    ingest["1 Ingest<br/>read, redact, index"]
    map["2 Map gaps<br/>topics and scores"]
    plan["3 Plan<br/>draft questions"]
    interview["4 Interview<br/>ask until complete"]
    synth["5 Synthesise<br/>notes to cited drafts"]
    approve["6 Approve<br/>approve, edit, reject"]
    use["7 Use<br/>search, ask, export"]

    ingest --> map --> plan --> interview --> synth --> approve --> use

    drafter(["drafter model"]) -.-> plan
    interviewer(["interviewer model"]) -.-> interview
    expert(["expert"]) -.->|approves the plan| plan
    expert -.->|answers| interview
    expert -.->|decides each entry| approve
    reader(["reader"]) -.-> use
```

| Stage | Command | Code | Model |
| --- | --- | --- | --- |
| 1 Ingest | `eki ingest` | `eki.ingest`, `mcp_servers` | none |
| 2 Map gaps | `eki map`, `eki gaps` | `eki.mapping` | none |
| 3 Plan | `eki plan` | `eki.interview.plan`, `eki.interview.drafting` | drafter (`EKI_MODEL_DRAFTER`) |
| 4 Interview | `eki interview` | `eki.interview.interviewer` | interviewer (`EKI_MODEL_INTERVIEWER`) |
| 5 Synthesise | automatic | `eki.runbook.synthesis` | none |
| 6 Approve | `eki entries`, `eki review` | `eki.runbook.review`, `eki.runbook.faithfulness` | none |
| 7 Use | `eki search`, `eki ask`, `eki export` | `eki.search`, `eki.runbook.export` | optional (`EKI_MODEL_EMBEDDING`, `EKI_MODEL_ANSWERER`) |

Stages 3 to 5 are one LangGraph workflow. Stages 1, 2, 6 and 7 are plain code
called from the command line or the API.

The gap score that orders stage 2 is
`100 x exclusivity x recency x (0.5 x doc_gap + 0.5 x criticality)`
(`src/eki/mapping/scoring.py`).

## Components

```mermaid
flowchart TB
    subgraph entry["Entry points"]
        cli["eki CLI<br/>src/eki/cli.py"]
        api["FastAPI app<br/>src/eki/api"]
    end

    subgraph core["eki package"]
        ingest["ingest<br/>pull, redact, index"]
        mapping["mapping<br/>topics, gap scores"]
        interview["interview<br/>plan, interviewer, store"]
        wf["graph<br/>LangGraph workflow"]
        runbook["runbook<br/>synthesis, review, export"]
        search["search<br/>hybrid search, answers"]
        llm["llm<br/>ChatModel, LiteLLM, tracing"]
        audit["audit<br/>append-only events"]
        authz["auth and access<br/>tokens, owner, readers"]
    end

    subgraph servers["MCP servers, read-only, one subprocess each"]
        git["git_server"]
        tickets["tickets_server"]
        mail["mail_server"]
    end

    subgraph external["Outside the process"]
        files[("source files<br/>git, tickets, wiki, mail")]
        pg[("Postgres 16 + pgvector")]
        provider(["model provider<br/>through LiteLLM"])
        langfuse(["Langfuse"])
    end

    cli --> ingest & mapping & interview & runbook & search
    api --> authz
    api --> interview & runbook & search
    ingest -->|stdio| git & tickets & mail
    git & tickets & mail --> files
    interview --> wf
    wf --> llm
    search --> llm
    llm --> provider
    llm -.->|best effort| langfuse
    ingest & mapping & interview & wf & runbook & search & audit & authz --> pg
```

Notes on the picture:

- **The CLI does not call the API.** Both call the same services
  (`InterviewService`, `RunbookService`, `SearchService`, `AnswerService`). The
  CLI is a trusted local operator tool with database credentials; untrusted
  readers use the API.
- **Every model call passes through the same wrappers**, outermost first:
  `TracedModel` (Langfuse, best effort) wraps `AuditedModel` (must succeed
  before the call) wraps `LiteLLMModel` (`src/eki/interview/service.py:106`).
- **MCP servers are separate processes** started per ingest over stdio
  (`src/eki/ingest/pipeline.py:423`). The mail server is started only when mail
  folders were named.
- **Langfuse** needs `EKI_LANGFUSE_PUBLIC_KEY` and `EKI_LANGFUSE_SECRET_KEY`;
  without them tracing is off (`src/eki/llm/tracing.py:104`). Audit does not
  depend on it.

## The interview workflow

Stages 3 to 5 are a fixed graph with one agentic node
(`src/eki/graph/workflow.py:237`). Only `interview_topic` lets a model choose
what to do next, and only through six tools.

```mermaid
stateDiagram-v2
    [*] --> draft_plan
    draft_plan --> await_review
    await_review --> next_topic: plan approved
    next_topic --> interview_topic: a topic is left
    interview_topic --> interview_topic: one question per step
    interview_topic --> check_complete: topic done
    check_complete --> interview_topic: a section is missing
    check_complete --> next_topic: every section noted or open
    next_topic --> synthesise: no topic left
    synthesise --> [*]

    note right of await_review
        interrupt: waits for the
        expert to approve the plan
    end note
    note right of interview_topic
        the one agentic node
        interrupt: waits for each answer
    end note
```

- **State is checkpointed in Postgres after every step**, one thread per plan
  (`thread_config`, `src/eki/graph/workflow.py:109`). A stopped process
  continues from the last question.
- **The record lives in tables, not in workflow state.** State holds control
  data and the model conversation of the current topic. Plans, turns, notes
  and open items are rows, written so that a repeated step rewrites its own
  rows instead of adding new ones (`src/eki/interview/store.py`).
- **A topic is complete** when each of the seven sections (trigger, diagnosis,
  steps, rollback, contacts, pitfalls, rationale) has a note or is recorded as
  open. `check_complete` is plain code, so the model cannot declare a topic
  finished on its own (`src/eki/graph/workflow.py:215`).
- **Limits per topic:** 12 questions and 40 model calls. When a limit is
  reached the remaining sections are marked open (`Limits`,
  `src/eki/graph/workflow.py:76`).

The interviewer's tools (`src/eki/interview/tools.py:30`):

| Tool | Effect |
| --- | --- |
| `search_artifacts` | reads ingested artifacts |
| `get_artifact` | reads one artifact |
| `ask_expert` | stores a question and waits |
| `record_note` | stores a note for one section |
| `mark_open` | records a section as unanswered |
| `finish_topic` | ends the topic if nothing is missing |

None of them approves, publishes, sends or writes to a source system.

The expert browser workspace (`eki.web`) is served by FastAPI and calls the
same authenticated API. Message receipts and stored redacted model replies
provide durable delivery; per-topic generation records provide drafting recovery.
See [message delivery](flows/message-delivery.md) and
[plan generation](flows/draft-plan.md) for their replay boundaries. Artifact
queries use the model identity saved with their run; explicit reindexing replaces
that index/model pair atomically.

## Runbook entries

One entry per plan topic, seven sections each. A section holds claims or one
open question. Every claim carries the citation it came from.

```mermaid
stateDiagram-v2
    [*] --> draft: synthesis
    draft --> approved: expert approves
    draft --> rejected: expert rejects, with a reason
    approved --> draft: edit, or a cited answer is corrected
    rejected --> draft: edit
    draft --> draft: edit, correction or re-synthesis
```

- Only `approved` entries are searchable, answerable and exportable.
- Every change increments the entry's version; the search index stores the
  version it was built from and ignores rows of any other version.
- Re-running synthesis rebuilds drafts from the notes. It never touches
  approved or rejected entries, and it keeps drafts the expert edited by hand
  unless `overwrite_edits` is set (`src/eki/runbook/service.py:54`).
- Approval requires a complete template and a citation on every claim
  (`src/eki/runbook/review.py:28`).

## Data model

Everything hangs off one ingest run. Deleting a run cascades to all of it,
except audit events, which are kept.

```mermaid
erDiagram
    ingest_runs ||--o{ artifacts : contains
    ingest_runs ||--o{ activity : summarises
    artifacts ||--o{ chunks : "indexed as"
    ingest_runs ||--o{ topics : "mapped to"
    topics ||--o{ topic_artifacts : groups
    artifacts ||--o{ topic_artifacts : "belongs to"
    topics ||--|| gap_scores : "scored by"
    ingest_runs ||--o{ interview_plans : "planned as"
    interview_plans ||--o{ plan_topics : lists
    plan_topics ||--o{ questions : drafts
    interview_plans ||--o| sessions : "opens one"
    sessions ||--o{ turns : records
    turns ||--o{ notes : "source of"
    sessions ||--o{ open_items : "leaves open"
    sessions ||--o{ runbook_entries : produces
    runbook_entries ||--o{ entry_decisions : "decided by"
    runbook_entries ||--o{ runbook_index : "searchable as"
    ingest_runs ||--o{ run_readers : "readable by"
    ingest_runs ||--o{ audit_events : "audited by"
```

| Tables | Migration | Written by |
| --- | --- | --- |
| `ingest_runs`, `artifacts`, `activity`, `chunks` | 0001, 0007 | `eki ingest` |
| `topics`, `topic_artifacts`, `gap_scores` | 0001 | `eki map` |
| `interview_plans`, `plan_topics`, `questions` | 0002 | plan drafting and review |
| `sessions`, `turns`, `notes`, `open_items` | 0002 | the interview |
| `runbook_entries`, `entry_decisions` | 0003, 0008 | synthesis and review |
| `run_readers`, `ingest_runs.owner_subject` | 0004 | `eki access`, access routes |
| `runbook_index` | 0005 | first search after an approval |
| `message_receipts`, `interview_replies` | 0009 | durable acceptance and replay of redacted model replies |
| `ingest_runs.artifact_embedding_model` | 0010 | artifact index/query model identity |
| `plan_generations`, `generation_topics` | 0011 | per-topic drafting progress and recovery |
| `audit_events` | 0006 | every model call, tool call and decision |
| LangGraph checkpoint tables | created by `eki migrate` | the workflow |

Migrations are in `src/eki/migrations/` and are applied in order by
`eki migrate`.

## Rules that hold everywhere

**Redaction comes first.** Secrets and personal email addresses are replaced
with `[REDACTED:kind]` before text is stored, indexed, logged, traced or sent
to a model (`redact`, `src/eki/ingest/redaction.py:82`). It is applied more
than once on purpose:

| Where | Code |
| --- | --- |
| In each MCP server, before a tool returns | `mcp_servers/common/source.py:77` |
| In the ingest client, again, on every artifact | `src/eki/ingest/pipeline.py:71` |
| On the expert's answers and corrections | `src/eki/interview/service.py:303` |
| On everything the model writes | `src/eki/interview/interviewer.py:111` |
| On every message sent to a provider | `src/eki/llm/litellm_model.py:58` |
| On every trace sent to Langfuse | `src/eki/llm/tracing.py:155` |
| On search queries and questions | `src/eki/search/service.py:88` |

Storage functions accept only the `RedactedText` type for anything a person or
a model wrote, so an unredacted string is a type error.

**Ingested content is data.** Artifact text reaches a model inside marked data
blocks, and the model is told not to follow instructions in it. The stronger
protection is structural: the interviewer has no tool that could act on such
an instruction.

**Confidential answers stay out.** An answer marked confidential is stored and
flagged. The model is told only that the section was answered
(`src/eki/interview/interviewer.py:196`); the runbook shows a fixed marker.

**Access.** API data routes need a bearer token from `EKI_API_TOKENS`. A run
has one owner and a list of readers. Owners and admins manage a run and see
drafts; readers see approved entries only; everyone else gets 404
(`src/eki/api/security.py:28`, `src/eki/access.py:14`).

**Audit.** Every model call, tool call and decision is written to
`audit_events` with run, user and call ID, and no content. The write must
succeed before a model or tool call is made (`src/eki/audit.py:210`).

## Not built, or not verified

- **Real sources.** The connectors read the synthetic local corpus only. There
  is no adapter for a live mailbox, GitHub or an issue tracker.
- **Real models.** Tests use scripted model doubles. Interview quality,
  semantic search and model-selected answers are unverified until the opt-in
  live tests are run with a provider key.
- **Plan editing from the CLI.** Removing, adding and reordering topics exists
  in the API (`PATCH /plans/{id}`) but not as a CLI command.
- **A user interface.** There is the CLI and the HTTP API, nothing else.
