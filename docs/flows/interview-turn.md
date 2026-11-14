# One interview turn

**Trigger:** a message from `eki interview --plan ID`, the browser conversation,
or `POST /sessions/{id}/messages`. **Outcome:** a durable expert turn and either
the next question or a finished session with private draft runbook entries.

Message acceptance and reconnect behavior are described in
[message-delivery.md](message-delivery.md). This page explains how a saved
submission becomes notes and the next question.

## What happens

```mermaid
sequenceDiagram
    actor Expert
    participant Svc as InterviewService
    participant WF as Interviewer node
    participant DB as Postgres
    participant LLM as Interviewer model

    Expert->>Svc: message ID, question target, answer/skip/correction
    Svc->>DB: acquire plan lock; validate target and deduplicate ID
    Note over Svc: redact before storage
    Svc->>DB: commit protected turn, receipt and audit together
    Svc-->>Expert: saved receipt (delivery event for explicit-ID clients)
    Svc->>DB: reconcile receipt with workflow checkpoint
    Svc->>WF: resume with the saved turn number
    WF->>DB: load protected expert turn
    Note over WF: confidential text is replaced by a marker before model context
    loop until another question or topic completion
        WF->>DB: load cached redacted reply for this step/call
        opt no saved reply
            WF->>DB: audit model attempt
            WF->>LLM: redacted conversation and six tools
            LLM-->>WF: tool calls
            WF->>DB: save redacted reply before applying its tools
        end
        WF->>DB: audited tool attempts; notes/open items/artifact reads
        WF-->>Expert: note/open/topic progress events
    end
    alt another question
        WF->>DB: store question; checkpoint at its interrupt
        Svc->>DB: mark receipt processed with terminal question
        Svc-->>Expert: question event
    else all topics complete
        WF->>DB: finish session and synthesise private drafts atomically
        WF->>DB: checkpoint ended workflow
        Svc->>DB: mark receipt processed with terminal done result
        Svc-->>Expert: done event
    end
```

A repeated processed receipt returns its stored terminal result before any
model or tool work. The diagram shows a new submission's processing path.

## Walkthrough

1. **Actions and access.** The service accepts `answer`, `skip` and `correct`.
   API/browser callers must own the run or be admins. The CLI remains a trusted
   local operator. One advisory lock on the plan serializes interview messages
   and holds through processing; a concurrent worker gets a conflict.
2. **Question binding.** Answers/skips name `question_turn`; corrections name
   `corrects`. The service validates the target against the durable transcript.
   Legacy callers can omit the ID/target when the waiting question is unambiguous.
   A different submission cannot overwrite an accepted answer.
3. **Durable acceptance.** The redacted turn, receipt, correction effects when
   applicable, and audit event commit together. The HTTP response starts only
   after that commit. `saved` means accepted, not finished or approved.
4. **Workflow recovery.** The service reconciles the target turn with its saved
   checkpoint. It recovers an interrupted step before delivering an answer when
   needed. The resume value carries a turn number, not answer text. A receipt
   that already advanced the workflow cannot accidentally answer the next question.
5. **Confidentiality and skip.** A skipped question is marked open. A confidential
   answer records a fixed note marker; the answer text is excluded from model
   input, reply caches, checkpoints, traces and API responses.
6. **Reply replay.** The interviewer reuses persisted redacted replies keyed by
   session, workflow step and call index. When no reply exists, the audited model
   is called and its result is saved before tools run. An identical note replay
   does not create another superseded note. Provider attempts without a persisted
   result can repeat and be billed again.
7. **Questions and completion.** `ask_expert` stores a question and stops that
   step's tool sequence; the next node invocation interrupts for its answer.
   Plain code checks every template section before a topic can finish. Missing
   sections return the model to the topic, subject to question/model-call limits.
8. **Final synthesis.** The last topic finishes the session and builds one draft
   entry per topic in the same transaction. No synthesis model runs. Procedure
   text uses the shared abbreviation/list-aware step splitter, and every claim
   cites its source turn. Repeating unchanged synthesis leaves versions alone.
   The delivery receipt is completed before its terminal result is returned.

Implementation: `eki.interview.service.InterviewService`,
`eki.interview.interviewer._Step`, `eki.interview.delivery`,
`eki.interview.store`, `eki.graph.workflow` and `eki.runbook.synthesis`.

## Corrections

The correction becomes a new expert turn. Notes from the corrected turn are
superseded and replaced word for word; affected entry claims are updated with
new citations, and affected entries return to draft for review. Procedure text
is split into ordered steps by the same helper used for synthesis; other
sections keep the correction as a whole claim. Unrelated claims are retained.
A correction does not answer the pending question. Its receipt and effects commit
atomically, so repeating its message ID does not add another review decision.

## Failure modes

| When | Behavior |
| --- | --- |
| Another worker holds the plan lock | 409; retry/resume after it finishes |
| Empty answer or invalid action/target fields | 422; no accepted submission |
| ID reused with changed canonical payload, stale question, or different outstanding submission | 409; saved answers are retained; resume or correct explicitly |
| New answer submitted to a finished interview | 409; corrections remain available |
| Model/audit processing pauses after acceptance | SSE `error` with `model_unavailable` or `audit_unavailable`; resume the saved receipt |
| Acceptance storage/audit fails before commit | No saved acknowledgement; the request fails before processing |
| Process stops between a write and its checkpoint | Resume the receipt/session; saved replies and idempotent writes recover the step |
| 12 questions or 40 model calls for a topic | Remaining sections are marked open and the topic ends |
| Model returns prose without a tool call | It is asked to use a tool; this counts toward the call limit |

The notes remain the model's wording; review compares them with the cited
answer and highlights unsupported terms. Artifact search uses the run's saved
embedding model, with lexical hashing as the default. See
[ingest.md](ingest.md) and [review-entry.md](review-entry.md).

Service and crash-recovery evidence uses scripted models and synthetic sources.
Real-model interview quality remains deferred by user request.
