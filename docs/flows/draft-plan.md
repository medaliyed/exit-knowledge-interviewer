# Draft or resume an interview plan

**Trigger:** `eki plan --run ID`, `POST /runs/{id}/plan`, or resume a saved
generation. **Outcome:** a complete draft plan, or owner-visible per-topic
progress that can be resumed without repeating completed topics.

## What happens

```mermaid
sequenceDiagram
    actor Operator
    participant Svc as InterviewService
    participant WF as Workflow
    participant DB as Postgres
    participant LLM as Drafter
    Operator->>Svc: draft_plan(run_id, generation_id)
    Svc->>DB: validate run and generation ownership; acquire generation lock
    Svc->>WF: start or recover the same workflow thread
    WF->>DB: load snapshot, mapping and any existing plan
    Note over WF: select ranked/contradicting topics; fingerprint inputs and configuration
    WF->>DB: save selection; reject changed inputs on resume
    loop each selected topic
        alt topic result already saved
            WF->>DB: load validated questions and dropped reasons
        else unfinished topic
            WF->>DB: audit model attempt
            WF->>LLM: source-grounded question request
            LLM-->>WF: candidate questions
            Note over WF: validate quotes; redact repair messages; retry invalid output once
            WF->>DB: persist completed topic result
        end
    end
    WF->>DB: publish plan and completed generation atomically
    WF->>DB: checkpoint at expert plan review
    Svc-->>Operator: complete draft plan
```

The implementation is in `eki.graph.workflow._draft_plan`,
`eki.interview.generation`, `eki.interview.drafting` and
`eki.interview.service.InterviewService.draft_plan`.

## Selection and question validation

The default selection is the ten highest gap scores plus any additional topic
whose artifacts contradict each other. Each topic supplies up to twelve redacted
artifacts to the drafter. Questions must name a shown artifact and quote it
verbatim; unusable candidates get one repair attempt. Rejected candidates and
all repair messages are redacted before storage or another model call. The
code-generated contradiction question remains first. A completed topic may have
no model questions; the interviewer can start from its description.

## Durable generation and recovery

Each generation uses a stable UUID, also its future plan ID and workflow thread
ID. The CLI prints it and the exact resume command before model work. New browser
clients choose the UUID before submitting; HTTP callers can pass
`?generation_id=UUID` to the plan trigger. Omitting it creates a new generation.

Before drafting, the workflow saves selected topic ordering and a fingerprint
of the stored snapshot/mapping, selected plan, model name, configured drafting
parameters, limits, work domains, actual system prompt and drafting/selection
code. Changed inputs or configuration refuse resume and require an explicit new
generation. Completed topic results retain their validated questions,
contradiction questions and dropped reasons. Each saves independently of the
next topic's model call and graph checkpoint.

Model errors retain both results and the workflow thread. Generation status is
`running`, `needs_retry` or `completed`. A dead worker releases its advisory
lock; inspecting its individual status recognizes abandoned work. Publishing the
complete plan and completed status is one transaction. Partial work has no
approvable `interview_plans` row. A crash after publication can recover the
review checkpoint without another drafting call.

## Operations and permissions

- `eki plan --run RUN_ID --generation-status GENERATION_ID` inspects progress
  without constructing a model client.
- `eki plan --run RUN_ID --resume-generation GENERATION_ID` resumes unfinished
  topics. Running `eki plan --run RUN_ID` without resume deliberately starts a
  new generation.
- `GET /runs/{id}/generations` lists recent generation records.
- `GET /runs/{id}/generations/{generation_id}` returns status, saved/total topic
  counts and partial questions. It includes `plan_id` only after completion.
- `POST /runs/{id}/generations/{generation_id}/resume` resumes an existing record.
  Unknown IDs are refused. A model-error response includes `generation_id` so
  clients can reconnect to its progress.

All generation routes are owner/admin-only through the parent run's ACL.
Cross-run generation IDs are rejected before processing. Concurrent resumes
share the generation/plan advisory lock and cannot draft the same topics in
parallel. The browser displays per-topic progress and offers inspect/resume
controls for interrupted generations. Listings are bounded to 200 records.

## Failure modes

| Failure | Behavior |
| --- | --- |
| Unknown or unfinished run | 404 before drafting |
| Run has no mapping | 409; no generation or plan published |
| Model unavailable | 502 with generation ID; completed topics and checkpoints retained |
| Inputs, model or prompt changed | 409; saved results retained; start a new generation explicitly |
| Another worker holds the generation lock | 409; no duplicate model work |
| Invalid question output twice | Topic result records remaining dropped reasons and any valid questions |
| Audit unavailable | 503 before provider calls; prior topic results remain |
| Partial generation approval attempted | 404; no approvable plan exists |

A call interrupted before its topic result is saved may repeat and be billed
again. Completed topics do not repeat. Real-provider quality remains pending by
user request. Migration `0011_generation.sql` adds generation records and topic
results; existing plans/sessions remain readable. Acceptance coverage is in
`tests/test_generation.py`.
