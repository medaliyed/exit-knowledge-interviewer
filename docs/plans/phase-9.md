# Phase 9 PDR: follow-up interviews on knowledge gaps

**Status:** proposed 2026-11-15, at the owner's request. Planning only;
implementation has not started. This document proposes behaviour and defaults.

**Parent:** [Project PDR](../PDR.md), new requirement **F22**. Reuses F6–F10
(plan, interview, confidentiality, citations and approval), F14/F18 (durable
messages and plan recovery), F13 (audit), F16/F19 (browser and identity), and
F20's budget/usage concepts. [Phase 8](phase-8.md) is a separate proposal for
maintenance after departure; it is not an implementation dependency.

**Goal:** while the original expert is still reachable, turn remaining open
sections and reader questions that Ask could not answer into a short, explicitly
approved follow-up interview. Publish the resulting knowledge through the
existing entry review process, with correct citations and bounded model use.

## 1. Problem and verified baseline

A completed interview can still leave useful questions unanswered. Ask can
expose another gap after readers start using the approved runbook. Neither
currently feeds a focused return to the expert.

| Current behaviour | Product gap / reusable part | Evidence |
| --- | --- | --- |
| Interviews store per-topic open items; runbook sections can contain an open question. | Open questions are visible but do not become a follow-up plan. | `src/eki/interview/models.py`, `src/eki/runbook/synthesis.py` |
| Ask returns `insufficient_evidence` and related open questions, without storing a question backlog. | A reader's unanswered question disappears when they leave the page. | `src/eki/search/answers.py`, `src/eki/api/app.py`, `frontend/src/Knowledge.tsx` |
| Ask searches approved entries across all permitted runs. Abstention can also result from changed access/version, bounded context or a selector declining to answer. | No answer is a useful signal, not proof that a particular expert lacks knowledge. Routing and triage must be explicit. | `AnswerService.ask()`, `docs/flows/search-and-ask.md` |
| The existing LangGraph workflow provides plan review, the interviewer and its six tools, message receipts, checkpoints, skip/confidential/correction and deterministic synthesis. | A second interview can reuse this machinery rather than add another agent or chat implementation. | `src/eki/graph/workflow.py`, `src/eki/interview/service.py` |
| Completion currently requires all seven sections for every topic. | Reusing it unchanged would repeat already answered questions instead of focusing on selected gaps. | `src/eki/interview/interviewer.py::missing`, `workflow._check_complete` |
| Synthesis creates one entry per session/topic; source validation accepts expert turns only from that entry's session. | Naive reuse would create duplicate entries or reject citations to the second session. | `src/eki/runbook/synthesis.py`, `src/eki/runbook/service.py::validate_sources` |

Reusing the workflow keeps the change focused. The additional work is gap
routing, a smaller completion scope, durable limits and explicit entry/citation
lineage; those are required for safe reuse and are not already implemented.

## 2. Users and outcome

| User | Outcome |
| --- | --- |
| Original expert / run owner | Reviews a small list of unresolved gaps, removes or rewrites questions, answers a short second session and approves resulting entry changes. |
| Reader | Saves an unanswered Ask question for a chosen handover and receives a durable receipt. Submission does not grant interview or publication access. |
| Administrator | Helps prepare a follow-up for an available expert without answering or approving on their behalf. |

Example: the first session captured month-end reconciliation but left rollback
open. A reader also asks who authorizes reopening a closed period; Ask cannot
support an answer. The reader saves that question for Nadia's handover. Nadia
selects both gaps, reviews a two-question plan and answers them. The resulting
proposal shows the added knowledge and its new citations beside the original
entry. Applying the proposal creates a draft of that entry; Nadia then approves
it. Readers retrieve the updated entry, not two competing month-end entries.

## 3. Scope

**In scope**

- A run-scoped queue combining public open sections and explicitly saved
  unanswered Ask questions, with owner triage and deterministic deduplication.
- A reviewed follow-up plan with selected topics/sections and frozen limits.
- A new session through the existing workflow, using the original expert and
  existing durable message delivery, controls and audit.
- Deterministic proposals for updating existing entries, or a normal private
  draft for a topic the expert deliberately adds.
- Explicit apply/review/approval, source lineage, concurrency checks, browser
  and CLI journeys, migrations and scripted acceptance evidence.

**Out of scope**

- Automatic invitations, email/calendar integration, reminders or scheduling.
  The owner starts the session while the expert is available.
- Automatic storage of every Ask question, automatic selection of an expert
  from a global search, or disclosure of private questions without a save action.
- Semantic/LLM deduplication, automatic topic discovery or an LLM judging whether
  a reader's question has been answered.
- A new interviewer agent, template, connector, source ingest or embedding model.
- Follow-up capture by a successor after handover. That is distinct from the
  maintenance review authority proposed in Phase 8.
- Automatic modification/approval of an entry or keeping multiple published
  versions of the same entry during review.

## 4. Proposed product decisions

### 4.1 Open sections are candidates; reader questions are saved explicitly

Derive open-section candidates from the current entry and its source state.
A candidate is an ordinary unresolved section, not an answered section, a
confidential-answer marker or a historical gap already filled by a later edit.
Owners may see candidates for their drafts and approved entries. Rejected
entries are excluded until separately reopened through review.

On an unanswered Ask result, provide **Save for a follow-up**. The reader sees
which handover's owner will receive the redacted question and chooses that run
explicitly. The current page's handover can be a suggestion, never an implicit
route for an Ask that covered multiple runs. Saving requires live access to the
chosen run. Questions remain 1–2000 characters, matching Ask's existing bound.

Saved text is an untrusted reader request, not evidence that the chosen run has
no relevant knowledge. Saving does not rerun Ask, search or a model merely to
verify that report. The owner may dismiss it, map it to an existing entry and
section, combine it with an open-section candidate, or deliberately add a new
topic with the normal seven-section template.

Expose a safe abstention reason in Ask results so the UI distinguishes a
knowledge request from an access/version change, an oversized context or a
review-required condition if Phase 8 exists. Provider errors and invalid inputs
are not knowledge gaps. A reader can still request clarification explicitly;
the queue never presents a client-supplied reason as a verified server finding.
No Ask history is stored merely to render the save button.

### 4.2 Make the follow-up short by construction

Proposed defaults: **three topics, six expert questions in total, and twelve
chat-model attempts per follow-up session**. Existing timeout and per-call output
bounds still apply. The expert sees these limits before approving the plan;
they may choose smaller limits or explicitly change them before approval.
Limits do not silently reset on resume or on a server restart.

Prepare the plan without a model: use the selected public open question or
saved reader question as a seed, retaining its source ID. The expert can edit,
remove and reorder it using the existing plan review. No drafter call is needed
just to reword a question already written down. Optional artifact context uses
the existing redacted source/index configuration.

For an existing entry, select only the sections the expert agrees to revisit.
Completed sections are read-only context, not new answers. Distinct questions
about the same section remain separate selected gaps unless the expert explicitly
groups them into one seed question. The interviewer may ask clarifying follow-ups
within the approved scope and budget. For a genuinely
new topic explicitly added by the expert, the normal seven-section scope
applies, with any unanswered sections left open when the session limit is hit.

A finished session can be complete or partial. Reaching a budget or receiving
“I don't know”, skip or confidential answers must not manufacture coverage.
Remaining gaps stay visible with their attempt outcome; another session requires
another explicit selection and approval rather than an automatic retry loop.

### 4.3 Preserve the entry until the expert applies a reviewed proposal

A new follow-up session has its own plan, transcript, checkpoints and receipts,
linked to a finished parent session in the same run. Each revisited topic also
records its target entry, original topic, selected sections and base version.

Finishing the session creates a private proposal, not a second published copy
of the target entry. Show original and proposed sections with citations. Keep
unselected sections and hand-edited claims unchanged. Replacing a selected
section that already has claims requires explicit expert acceptance of the
section diff; the model does not merge prose or silently drop existing claims.

Applying a proposal updates the existing entry ID to draft and invalidates its
published index, matching today's edit behaviour. A separate explicit approval
makes that new version reader-visible. Until apply, the existing approved entry
remains available; the UI labels the follow-up proposal as unpublished.
A genuinely new topic receives a normal private draft once, with no automatic
approval. Confidential-only or unknown answers do not produce publishable claims.

## 5. Requirements

### 5.1 Gap intake and triage

| ID | Requirement |
| --- | --- |
| G1 | The owner/admin queue is scoped to a run and combines eligible current open sections with saved Ask requests. Recheck entry status/version and source confidentiality before selection; do not derive eligibility by searching for the word “confidential” in free text. |
| G2 | Persist an Ask request only after an explicit save to a permitted run. Store redacted question, requester subject, time, source kind and optional permitted entry/section reference. Referenced entries must belong to that run and be readable by the requester; global Ask results from another run must not be copied into its context. |
| G3 | A client UUID makes saves idempotent. Identical normalized requests under that ID return the same receipt; a changed payload returns 409. Collapse exact repeated redacted questions by the same requester within the same run; no semantic model call is made. Owner grouping of multiple sources preserves each source/receipt. |
| G4 | Apply a shared-worker limit of ten new reader requests per minute per subject; retries do not count as new submissions. Readers may view only their own safe receipts under live run readership, not the owner's backlog, another requester's identity or private interview outcomes. Revoked access returns 404. |
| G5 | Triage records selected target entry/section or explicit new-topic choice, edited seed question, priority and a redacted deferral/dismissal reason. Ask text is data, not an instruction to tools. Only owner-approved seeds and necessary redacted source context reach the interviewer; reader identity is omitted. |
| G6 | Track `pending`, `scheduled`, `awaiting_review`, `resolved`, `deferred` and `dismissed`, plus attempt outcomes. Scheduling is not resolution. Skip, unknown, limit stops and confidential-only answers remain unresolved; they do not automatically start another attempt. |
| G7 | Resolve an open-section candidate only after a target revision containing public cited knowledge is approved and that section is no longer open. Resolve an Ask request only when the reviewer explicitly links it to an approved entry/version and confirms it addresses the request. Approval of an unrelated entry or a model's assertion does not close it. |

### 5.2 Plan, participant and session scope

| ID | Requirement |
| --- | --- |
| I1 | Follow-up plans/sessions record `kind=follow_up`, parent session, run, selected gap IDs and target mappings, original expert identity, frozen limits and plan revision. The parent must be finished and in the same run; an ordinary interrupted interview is resumed through F14, not cloned as a follow-up. |
| I2 | Owner/admin can prepare a draft plan without provider work. Starting it requires the original expert's authenticated approval using the capture expert check and current owner subject. Record that subject as the respondent; a same-name account, admin or reader cannot answer on their behalf. Recheck identity, active-account status and ownership on writes and resume. This adds no interview-participant role or ACL widening. |
| I3 | Generate seed questions deterministically, with an explicit follow-up origin referencing their selected gap(s). Seed count cannot exceed the approved total question limit; the expert must group, remove or defer excess items. Do not fake an artifact quote to satisfy the ordinary drafter's F6 format. Normal artifact-triggered questions retain quote validation. Creation/approval retries return the same plan/session for the same request, not duplicates. |
| I4 | Only one active follow-up per run is allowed initially. Atomically reserve its selected gaps at plan approval so concurrent starts cannot interview the same items twice. Removing a gap from an unapproved plan releases its selection; stopping a session retains its transcript and returns unresolved work to triage with attempt history. |
| I5 | Reuse the existing LangGraph nodes and six-tool interviewer. Apply one persisted section scope consistently to prompts, checklist queries, note/open tools, completion checks and synthesis. Unselected sections are outside scope, not falsely recorded as answered/open/confidential in the new session. Notes for unselected sections are refused. |
| I6 | Reuse F14 message IDs, question binding, accepted-answer persistence, reply/tool replay and explicit delivery state. Skip, correction and confidential controls remain available. No confidential content enters proposals, prompts, traces, reader receipts or responses. A confidential answer cannot be used to fill a public gap. |
| I7 | Questions and chat/provider attempts have durable session-wide counters in addition to existing per-topic limits. A completed cached reply replay spends no new provider allowance; an uncertain/crashed attempt remains charged conservatively. A fresh wrapper in each HTTP request must not reset the allowance. No provider call begins if audit/budget reservation fails. |
| I8 | A question limit prevents asking question N+1, but allows processing the Nth accepted answer within the remaining call allowance. A model budget stop closes the interview through deterministic code with a partial completion reason; accepted answers/receipts remain durable and closure is committed before delivery is reported processed. It cannot leave an endlessly retrying paused session or falsely mark uncaptured facts as noted. |
| I9 | Report question count, model attempts, available input/output tokens, duration and `completion_reason` (`complete`, `question_limit`, `model_budget`, `expert_stopped`). Optional semantic retrieval uses the recorded matching index/model pair, with a separate durable default cap of six provider embedding requests; local hashing is not substituted into a different model's index. No ingest/reindex is triggered by follow-up preparation. |
| I10 | Track selected seed attempts independently of section coverage. The first question for each selected seed uses its approved text/ID; later clarifiers retain that gap ID. Finishing a section does not imply other selected questions in it were asked or answered. Require each seed to be attempted or explicitly left unattempted by a limit/stop. Extend the existing tool arguments/validation with follow-up gap attribution, without adding a seventh tool. Preserve public facts from multiple answers in the same section with their individual source turns instead of overwriting the first note. |

### 5.3 Proposals, citations and publication

| ID | Requirement |
| --- | --- |
| P1 | Synthesis branches deterministically for follow-up sessions: mapped topics create idempotent proposals for their target entry; explicitly new topics create ordinary private draft entries. Replays do not create duplicate proposals, entries, decisions or indexes. Normal capture synthesis is unchanged. |
| P2 | A proposal holds the base entry/version, selected section patch, source gap IDs and public follow-up citations. Unselected sections are copied from the reviewed base, preserving order, claims, open questions and citations exactly. Skipped/unknown/confidential-only outcomes create no public fact patch and do not clear an open question. |
| P3 | Existing-entry apply requires explicit selection of the section changes and an expected base version. Under lock, reject a changed/edited/rejected/deleted target or changed proposal with 409 and preserve the proposal for comparison. No automatic rebasing over human edits or approvals. A target draft can be updated only through the same explicit reviewed diff. |
| P4 | Applied content keeps the target entry ID, increments its normal version, clears approval, saves as draft and invalidates its index atomically. Explicit approval by the current entry-review authority revalidates every section/source before readers can search, ask or export it: the original expert in capture, or the current maintenance owner if Phase 8 is implemented. Hand-edited drafts cannot be overwritten by an ordinary re-synthesis. |
| P5 | Extend source validation through an explicit entry-to-follow-up/topic lineage. Claims may cite public expert turns from the original session or authorized linked follow-ups for that entry. Same run alone is insufficient. Reject unrelated-session/topic, corrected, skipped, confidential or forged sources. Preserve citation session IDs; never copy answers into the first session to pretend they were said there. |
| P6 | Extend correction propagation and faithfulness checks to linked sessions. Correcting any cited follow-up turn must find affected target entries/proposals, replace or invalidate dependent claims, reopen approval and invalidate published indexes atomically, just as original-turn correction does. Preserve unrelated claims; do not rely on the current same-session-only entry scan. |
| P7 | Citation rendering, owner transcript expansion, search, Q&A and Markdown resolve the explicit lineage while retaining live ACL, approval and version gates. Readers still receive safe citation references rather than transcript access. New interview answers do not become searchable through a draft proposal or gap receipt. |

### 5.4 Concurrency, privacy and Phase 8 compatibility

| ID | Requirement |
| --- | --- |
| C1 | Use shared authorization and transition services across HTTP, CLI and browser. Serialize run ownership/mode checks, source/target session locks in stable ID order, target entries, proposals and gap reservations in a documented order compatible with capture/review/correction paths. Validate expected versions again in the committing transaction. |
| C2 | Every persisted question, edited seed, answer and reason is redacted before storage or model use. Audit uses allowlisted actor/target IDs, action, versions, counters, budgets and outcomes only, never question/answer bodies, reasons, credentials or confidential text. State changes and their audit events commit together. |
| C3 | An Ask abstention reason distinguishes at least no supported answer, evidence/access change and bounded-context limits; add review-required handling if Phase 8 is implemented. Do not reveal the presence of inaccessible entries/runs. Errors are errors rather than manufactured gap records; saving a request does not claim that no evidence exists anywhere. |
| C4 | Cookie mutations retain Origin/CSRF protection and all identity/ACL checks apply before reads, aggregation and writes. Reader-submitted questions do not grant session, source, plan, proposal, audit or draft access. Counts and receipts cannot expose another run or reader. |
| C5 | Integrate with Phase 8 without requiring it: maintenance mode refuses new follow-up capture, plan approval and answer writes. Handover respects active follow-up reservations and accepted receipts; finish/stop the session and settle accepted work before transfer. Preserve captured proposals and unresolved requests for successor triage. Where Phase 8 is implemented, its current owner may apply/review an already captured proposal under normal maintenance authority without changing who answered or reopening the interview. |
| C6 | If Phase 8 review dates exist, preparing/answering/applying a follow-up does not renew them or resolve reader flags. Only the normal explicit approval/review sets a new deadline. A freshness-only abstention belongs in maintenance review rather than automatically prompting the expert again. |
| C7 | Migrations leave existing plans/sessions as capture, preserve transcripts/entries/citations/approvals, and do not fabricate past Ask history or schedule sessions. Derive open candidates from current records. The feature adds no provider call until an expert starts or resumes approved interview work. |

## 6. API and CLI proposal

These are proposed routes and strict request models, not implemented contracts.
Actor identity is derived from authentication. Owner/admin queue endpoints and
reader receipts have separate response models.

| Surface | Proposed operation |
| --- | --- |
| `POST /runbook/ask` | Add a safe abstention `reason` and return normal evidence/open questions as today. Default Ask does not persist its question. |
| `POST /runs/{id}/follow-up-requests` | Reader/owner explicitly saves `{request_id, question, entry_id?, section?}`; validates destination access. 201 new, 200 identical replay, 409 changed replay. Source is a reader-reported unanswered question, not verified absence of knowledge. |
| `GET /follow-up-requests/{id}` | Own safe receipt or scoped manager view. No draft details or another reader's text/identity. |
| `GET /runs/{id}/follow-up-gaps` | Owner/admin paginated current open sections and saved requests, with filters, attempt history and source versions. |
| `PATCH /runs/{id}/follow-up-gaps/{gap_id}` | Owner triage: map/group, defer or dismiss with expected revision and required reason where relevant. |
| `POST /runs/{id}/follow-ups` | Owner/admin prepares `{request_id, parent_session_id, selected_gap_ids, targets, limits}`; validates scope and prepares deterministic seed questions without model calls. |
| Existing plan/session routes | Review and approve the follow-up, then answer/resume with existing receipts/SSE. Enforce respondent, selected scope and durable global budgets in shared services. |
| `POST /sessions/{id}/stop` | Authorized expert stops further questions, settling accepted work and finishing with unresolved items retained. A processing receipt must be recovered before stop commits; stop cannot discard an accepted answer. |
| `GET /sessions/{id}/follow-up-proposals` | Expert/manager comparison of base and proposed sections with citations and versions. |
| `POST /follow-up-proposals/{id}/apply` | Current entry-review authority selects section patches with expected proposal/base-entry versions; create the target draft atomically. |
| Existing entry approval route | Current entry-review authority explicitly approves; link eligible gap resolutions to the resulting approved version. |

CLI additions: `eki follow-up gaps --run ID` and `eki follow-up plan --run ID
--parent-session ID --gap GAP_ID` with explicit limits. Continue through
`eki interview --plan ID`; expose proposal comparison/apply and the existing
entry review. Read-only queue/plan preparation needs no model configuration;
interview execution uses the normal configured provider. CLI mutations must
use the same participant/ownership rules as HTTP, not a free-form reviewer name
that bypasses respondent checks.

## 7. Browser flow

1. After the first session finishes, show **Plan a follow-up** with the current
   unresolved public sections. Empty state: “No public gaps to follow up.”
2. An unanswered Ask shows **Save for a follow-up**, the chosen handover and who
   receives the question. Saving confirms that it is a request awaiting triage,
   not an appointment or promise of an answer. Technical errors suggest retry;
   review-required abstentions point to maintenance where available.
3. The owner selects/groups gaps, maps questions to entries/sections or adds a
   new topic, then reviews the usual plan with its scope and visible budget.
   A missing/disabled/departed expert is shown as unavailable, without starting
   model work or switching to a successor silently.
4. The existing interview screen identifies the session as follow-up, shows its
   selected sections and remaining question budget, and offers the normal
   answer/skip/confidential/correction/recovery controls plus **Stop for now**.
5. Completion shows captured sections, remaining gaps and unpublished proposals.
   The expert inspects each diff, applies selected changes to draft and then
   explicitly approves using the existing entry review.

Keyboard/focus recovery, partial saves, stale-version conflicts, narrow layouts
and clear confidential/unknown outcomes belong in the browser acceptance pass.
Do not claim a time estimate such as “five minutes” based solely on six questions.

## 8. Data and implementation approach

Additive migrations, numbered when implementation begins, cover saved reader
requests and safe receipts, gap selections/attempts, follow-up plan/session
lineage and scope, frozen limits with durable usage reservations, entry target
mappings, per-seed attempt/answer attribution, public facts that retain each
source turn, and private proposals with source/base versions. Keep new records
under the existing run's ACL; no new public search index is introduced.

Use the existing plan model/review, `InterviewService`, LangGraph graph,
message delivery store, interviewer tools and synthesis/review services.
Extend their deterministic setup, scope and completion/synthesis paths rather
than adding an independent conversation loop. Ordinary capture retains its
seven-section checklist and stable recovery behaviour.

Phase 7's in-memory eval budget cannot enforce a resumable HTTP session's total
allowance by itself. Adapt the budget concept using durable reservations linked
to model/audit call IDs and cached reply recovery. Report chat and provider
embedding usage separately. Hard call limits and existing output bounds cap
exposure; this phase makes no fixed currency-cost promise across providers.

Cross-session citation support includes reverse lookup of dependent claims and
proposals for corrections, not just accepting another session ID. This is the
main runbook change. Published retrieval continues to use the current approved
version of the existing entry; proposal text and reader questions are excluded.
No new dependency or source connector is needed.

## 9. Acceptance criteria

| # | Criterion |
| --- | --- |
| A1 | A completed synthetic interview with one ordinary open rollback and one confidential section produces only the eligible public gap. Filling/closing it in a later entry revision removes it from current candidates; historical opens are not re-added. |
| A2 | Ask still performs no question-history write by default. A reader explicitly saves a redacted unanswered question to a permitted run; changing the selected page/run does not silently reroute a global Ask. Errors, stale access/context and review-required reasons are presented distinctly. |
| A3 | Identical saves/retries produce one request/receipt. Changed IDs/payload combinations, limits, revocation and concurrent-worker throttling behave as specified. Another reader's requests, draft information and inaccessible run counts remain hidden. |
| A4 | Preparing/reviewing a follow-up calls no model and uses only selected gaps. Only the authorized original expert can start/answer it; readers, a same-name different subject and a non-owner admin cannot impersonate them. Parallel creates/approvals yield one active session/reservation. |
| A5 | With the scripted interviewer, a rollback-only follow-up asks no already-covered sections, uses the normal receipt/checkpoint/recovery path and produces the selected public note. Unselected-section tool writes and forged source/target mappings are refused. Two distinct selected questions in one section are both attempted and retain their separate facts/citations; one section note cannot silently satisfy both. A deliberately added new topic uses the normal template. |
| A6 | Defaults enforce six total questions across topics, twelve chat attempts and bounded embedding attempts across disconnect/restart/retry. Cached replies are not billed by the counter twice; uncertain attempts are retained. Exhaustion/stop/unknown/confidential outcomes finish explicitly partial, preserve accepted work and leave affected gaps unresolved. |
| A7 | Finishing creates a private proposal and leaves the original approved entry untouched. Applying selected sections preserves unrelated/hand-edited claims and citations, updates the same entry ID to draft and removes it from reader retrieval. Explicit approval makes only the revised entry visible, with genuine second-session citations and no duplicate published topic. |
| A8 | A concurrent target edit/rejection/correction/approval causes apply to conflict without overwriting it. Replays create no duplicate proposals, entries, decisions or index effects. Explicit proposal refresh/review is required after a changed base. |
| A9 | Citations accept only explicitly linked public expert turns for the target entry/topic and retain original session IDs. Unrelated sessions, corrected/skipped/confidential turns fail. A correction to a cited follow-up answer updates dependent original entries/proposals and reopens approval atomically, preserving unrelated claims. |
| A10 | A candidate is not resolved merely by scheduling, finishing or drafting an answer. Approval plus the specified open-section check or explicit Ask-request resolution is required. Skips, unknowns and confidential-only answers do not trigger repeat interviews or claim an answer was published. |
| A11 | Handover/mode or ownership changes racing active follow-up writes cannot admit an unauthorized commit or discard accepted answers. Under Phase 8, maintenance blocks new capture; a successor can review an already captured proposal under maintenance authority with honest original-expert attribution. Follow-up work never implicitly renews review dates or resolves reader flags. |
| A12 | Audit failure rolls back the associated mutation; raw question bodies, answers, reasons and credentials do not enter audit/logs. Browser owner/reader journeys, redaction/adversarial cases, lock ordering and migration compatibility pass. Existing pytest unit/database/red-team/eval suites, Ruff, mypy, frontend checks and relevant browser tests pass without paid providers. |

## 10. Deliverables and order

1. **9a — Gap intake and triage.** Current public-open candidates, explicit saved
   Ask questions, safe reasons, receipts, deduplication, throttling and ACL tests
   (G1–G7, relevant C1–C4/C7, A1–A3/A10).
2. **9b — Focused workflow and durable budgets.** Deterministic plan preparation,
   participant/scope checks, existing workflow integration, global usage limits,
   stop/recovery and scripted database tests (I1–I10, A4–A6/A11).
3. **9c — Entry proposals and source lineage.** Target diffs/apply, explicit
   approval, cross-session citation validation, correction propagation and
   publication/regression tests (P1–P7, A7–A10).
4. **9d — Browser, CLI and documentation.** Intake, gap selection, follow-up
   interview, comparison/review, operational guides and complete evidence
   (A12). Record actual scripted and any later paid verification separately.

Use F22 in deliverable commits. Leave human-owned `evals/scenarios/` and
`evals/golden/` untouched; add deterministic acceptance cases under `tests/`.
Implementation does not require or authorize a paid model evaluation.

## 11. Decisions to confirm with phase approval

- Reader questions enter the backlog only through explicit save to a selected
  handover; open public sections are derived automatically.
- Defaults of three topics, six total questions, twelve durable chat attempts
  and six provider embedding requests; actual limits are visible and frozen
  when the expert approves the plan.
- Existing-entry answers produce an unpublished proposal, then explicit apply
  to draft and separate approval; prior approved content stays available only
  until apply, matching the product's current publication model.
- Follow-up sessions use the original expert before handover; successor
  maintenance and reader flags retain Phase 8's separate responsibilities.

Status will record implementation approval and any changes to these choices.
Creating this proposal does not change runtime behaviour or contact an expert.
