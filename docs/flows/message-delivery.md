# Message delivery and recovery

**Trigger:** `POST /sessions/{id}/messages`, receipt resume or session resume.
**Outcome:** an accepted message stays in the protected transcript, with a
metadata-only delivery receipt, until the workflow reaches its next question or
finishes. A retry reuses that submission instead of storing another answer.

## Acceptance

New clients generate `message_id` once and send it with the intended
`question_turn` for answers/skips, or `corrects` for corrections. Under the
session's advisory lock, the service validates the target and redacts text.
The expert turn, receipt, any correction effects and acceptance audit event
commit in one transaction before the HTTP response starts. The response header
`X-EKI-Message-ID` identifies the saved submission. Explicit-ID clients also get
a `delivery` SSE event; legacy callers retain their earlier progress events.

Same ID and canonical redacted payload returns the saved submission/result.
Changed payloads, a second answer to an answered question and stale targets
conflict. Existing callers can omit IDs; the server assigns one. Transport
retries should use that returned ID. A legacy retry without an ID can match an
outstanding saved submission but cannot overwrite it with different text.

## Processing and reconnect

Receipts move from `saved` to `processing`, then `processed` after a terminal
question/done result is committed. Provider/audit failures and disconnects leave
recoverable work; status becomes `needs_retry` when storage is available. A hard
process death releases its advisory lock; status reads recognize abandoned
`processing` receipts as needing retry.

Connection opening, dependency lookups, queries and connection closing run in
database workers, so slow Postgres operations do not block the API event loop.
The stream retains its connection and session lock until completion or explicit
close, including close before the first event. Cancellation waits for the active
database worker before closing the connection or releasing its locks; cancelled
connection attempts close any connection they finish creating. Accepted answers
remain recoverable through the same receipts. This does not cancel an already
running SQL statement or change the replay boundaries below.

- `GET /sessions/{id}/messages` lists outstanding receipts.
- `GET /sessions/{id}/messages/{message_id}` returns status, turn/target IDs and
  the terminal result. It never returns the expert's message text.
- `POST /sessions/{id}/messages/{message_id}/resume` resumes that receipt with no
  message body. An already processed receipt returns its stored terminal result.
- `POST /sessions/{id}/resume` resumes outstanding delivery or recovers an
  interrupted workflow step, such as a first question not yet checkpointed.

All routes require owner/admin access, as other session routes do. The CLI
shows receipt IDs and offers `/retry` when it finds outstanding delivery. The
[browser workspace](browser-workspace.md) retains submission IDs for unchanged
transport retries, checks receipts when a response is lost, and exposes
**Resume saved answer** on reconnect.
If there is no valid question to answer, submitting new text is refused; use
resume first. There is no background worker: reconnect/retry drives recovery.

## Replay boundaries

The service reconciles the saved turn with the workflow's pending question.
If the question was written before its checkpoint, it recovers that checkpoint
before delivering the answer. If the checkpoint already advanced, it records
the terminal result without answering the next question accidentally.

Redacted model replies are persisted before their tools run, keyed by session,
workflow step and call index. After a crash mid-tool-sequence, the node reuses
the stored reply. Repeated identical notes and synthesis do not add history or
increment entry versions. Corrections and their receipt commit together, so
retrying a correction cannot repeat review decisions.

Confidential answer text remains only in protected transcript storage. Receipts,
audit events and checkpoints carry references or markers. A provider call that
never produced a durable reply may repeat and be billed again. Delivery protects
logical database effects; it does not guarantee exactly-once external execution.

Implementation: `eki.interview.delivery`, `eki.interview.service`,
`eki.interview.interviewer`, migration `0009_delivery.sql`.
Acceptance coverage: `tests/test_delivery.py`.
