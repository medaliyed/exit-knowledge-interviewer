# Phase 8 PDR: knowledge maintenance after the expert leaves

**Status:** implementation approved 2026-11-15 by the owner's request to
continue Phase 8. Work is in progress through deliverables 8a–8d; verification
and remaining acceptance will be recorded in Outcome.

**Parent:** [Project PDR](../PDR.md), new requirement **F21**. Extends F10
(review and approval), F11/F12 (retrieval and export), F13 (audit), F16 (browser
workflow) and F19 (authenticated identities).

**Goal:** a runbook remains maintainable after its original expert leaves.
An explicitly assigned successor can correct and re-approve entries, each
approved entry has a review-by date, and readers can report problems to an
accountable owner. Original authorship and evidence remain visible.

## 1. Problem and verified baseline

Today the product captures knowledge but does not provide its ongoing care.
Changing the run owner does not transfer the ability to review its entries.

| Current behaviour | Consequence | Evidence |
| --- | --- | --- |
| HTTP checks that a caller manages the run, then `RunbookService._change()` and `review.approve()` compare the reviewer name with `ingest_runs.expert_name`. | A successor with a different name cannot edit, reject or approve, even after an administrator transfers run ownership. | `src/eki/api/security.py`, `src/eki/runbook/service.py`, `src/eki/runbook/review.py` |
| Editing an approved entry or correcting a cited answer returns it to draft and invalidates its index. | Once the expert is unavailable, the entry can remain unpublished indefinitely. | `src/eki/runbook/service.py`, `src/eki/runbook/store.py` |
| Entries have approval metadata and a version, but no review deadline. | An old approval continues to look current forever. | `src/eki/runbook/models.py`, `src/eki/migrations/0003_runbook.sql` |
| Readers can read approved entries but cannot report a problem in the product. | The maintainer cannot see an actionable queue of reader concerns. | `src/eki/api/security.py`, `frontend/src/types.ts` |
| Entry edits are stored as expert turns, and source validation accepts only expert turns. | Simply removing the name check would misattribute successor-authored corrections to the original expert. | `src/eki/runbook/service.py::edit`, `validate_sources()` |
| Ownership already uses a stable subject and can be changed only by an administrator or trusted local operator. | There is an existing ownership mechanism to extend; another independent owner field would create conflicting authority. | `src/eki/access.py`, `src/eki/cli.py` |

The current capture workflow, seven-section template, explicit publication
approval, redaction, confidential-answer isolation, run ACLs and metadata-only
audit remain the baseline. This phase adds a maintenance stage after capture;
it does not claim that a passing review proves operational correctness.

## 2. Users and outcomes

| User | Outcome |
| --- | --- |
| Original expert | Hands over responsibility without changing their historical answers or approvals. |
| Successor owner | Sees overdue entries and reader flags, updates cited knowledge and publishes a reviewed revision under their own identity. |
| Reader | Sees whether an entry is due for review, reports a concrete problem and can follow their report's disposition. |
| Administrator / local operator | Assigns or recovers ownership when the expert has already left, with a recorded reason and no need for the expert's credentials. |

Example: Nadia approved the failover entry. An administrator hands the run to
Samir. A reader reports that a command is obsolete. Samir edits the steps;
the entry becomes draft and disappears from reader retrieval. His new text
cites an attributed maintainer edit, not an answer attributed to Nadia. Samir
approves the revision and sets its next review date. The entry becomes visible
again, retaining Nadia's original source history and Samir's maintenance history.

## 3. Scope

**In scope**

- An explicit, audited handover into maintenance, including recovery when the
  expert is unavailable, using the existing run owner subject.
- Successor review authority enforced in the service, HTTP API and CLI.
- Honest attribution and citations for successor-authored entry edits.
- Per-entry review-by dates, an unchanged-content review action and a queue
  for due-soon, overdue and flagged entries.
- Reader flags with owner triage and explicit resolution.
- Browser, search, Q&A and Markdown presentation of maintenance metadata.
- Migration, concurrency protection, audit and deterministic acceptance tests.

**Out of scope**

- Automatic emails, Slack messages, reminders, scheduled jobs or escalation
  chains. The queue and visible dates provide the first maintenance workflow.
- Multiple owners, per-entry ownership overrides, teams or delegate roles.
  One run has one current owner; later transfers use the same mechanism.
- A complete migration of the capture stage's legacy name-based review model.
  Maintenance authority uses stable subjects; capture retains its existing
  expert checks until an explicit handover.
- Automatic factual verification, LLM-written corrections or automatic approval.
- Publishing reader comments as knowledge, general discussion threads or votes.
- Live-source change detection, automatic withdrawal on a flag, restoring old
  published versions during edits, or an archive of all historical entry bodies.
- Reopening the interview or rewriting the original expert's answers after handover.
  Follow-ups before departure are proposed separately in [Phase 9](phase-9.md);
  its active sessions and accepted messages must settle before handover.

## 4. Proposed product decisions

### 4.1 One owner, with an explicit maintenance handover

`ingest_runs.owner_subject` remains the single source of current ownership.
Add a maintenance-start marker and append-only handover history. A handover
atomically sets the owner and enters maintenance. Later ownership changes
transfer maintenance authority immediately and retain that history.

The original expert name and source corpus identity never change. In capture,
existing expert review rules continue to apply. In maintenance, only the
current owner's authenticated subject authorizes entry review. A matching name,
a historical approval, a former owner role or administrator status alone does
not grant maintenance approval authority. Administrators can assign themselves
as owner explicitly if they need to take responsibility.

Only an administrator or trusted local operator performs a transfer in this
phase. The expert can request a handover operationally; the product does not
send that request. The destination must be an active account or a subject in
the configured bearer identity registry. Disabled accounts are ineligible.
Changing a display name does not change authority.

Handover is permitted with approved entries, drafts, rejected entries and
unfinished capture work. It does not publish outstanding drafts or renew dates.
Entering maintenance closes capture for further plan approval, answers and
transcript corrections; persisted work remains inspectable through its existing
ACL and confidentiality filters. Outstanding accepted messages must settle or
be explicitly recovered before the transfer commits. Account disablement and
session revocation remain separate F19 operations; a transfer does not silently
disable an account used for other runs.

### 4.2 Review dates express freshness, separately from publication

Keep `draft`, `approved` and `rejected` as publication states. Compute freshness
from the approved entry's `review_by` date and the server's UTC date:

| Freshness | Definition | Reader behaviour |
| --- | --- | --- |
| Current | More than 14 days remain before `review_by`. | Normal approved entry with review metadata. |
| Due soon | Today is on or before `review_by`, and no more than 14 days remain. | Visible date and due-soon badge. |
| Overdue | Today is after `review_by`. | Still readable/searchable/exportable as approved, with a prominent “Review overdue” label. |

A date is inclusive through the end of that UTC day. “Overdue” means a human
review is late, not that the procedure has been proven incorrect. Drafts and
rejected entries have no reader-facing freshness state.

Proposed default: every new approval or explicit unchanged-content review sets
`review_by` to today plus 90 days, unless the reviewer chooses an explicit date
from tomorrow through 365 days ahead. Extending a date requires a review action;
a metadata-only date patch cannot postpone accountability.

Overdue entries remain in search with labels, but are excluded from Q&A's
candidate evidence before ranking/context assembly and before any model call.
If no eligible evidence remains, Q&A abstains with `insufficient_evidence` and
an explanation that permitted material needs review. No opt-in to stale Q&A is
introduced here. This changes answer eligibility intentionally; it prevents an
old approval from silently powering an apparently current answer.

Reader flags alone do not remove entries or establish that they are wrong.
They produce a visible “Reader feedback pending” signal and an owner queue.
The owner can withdraw a suspect approved entry to draft immediately, without
inventing a content edit, while checking the report.

### 4.3 Reader reports require an owner decision

A flag records the observed entry version, optional section, one category
(`incorrect`, `outdated`, `unclear`) and a required redacted description of
1–2000 characters. A reader flags an entry they can currently read; they cannot
edit it, change its dates or force its withdrawal.

A flag is `open` until the owner resolves it with one of:

| Resolution | Required evidence/action |
| --- | --- |
| Corrected | Link to a later approved entry version; record what was addressed. |
| Confirmed current | Complete an explicit review of the current approved version, then record why the report does not require a correction. |
| Dismissed | Record a reason, for example a duplicate or a concern unrelated to this entry. Dismissal alone does not renew the review date. |

Editing, approving, renewing a date or handing over a run never silently closes
flags. Reports on older versions remain open until explicitly triaged; the
queue distinguishes their observed version from the current one. A resolved
flag is immutable; new information is submitted as a new flag.

## 5. Requirements

### 5.1 Handover, authority and provenance

| ID | Requirement |
| --- | --- |
| H1 | A transfer records run ID, previous and new owner subjects, actor subject, time, redacted reason and whether it started maintenance. An expected current owner/revision prevents a stale transfer from overwriting a newer one. |
| H2 | API and CLI ownership paths share the transfer service. The existing `PUT /runs/{id}/owner` and `eki access --owner` cannot bypass maintenance history, identity validation or concurrency checks. An ordinary owner cannot transfer ownership. |
| H3 | In maintenance, approve, edit, reject, withdraw, unchanged-content review and flag resolution require the current owner subject. Recheck this inside the write transaction; middleware authorization alone is insufficient. |
| H4 | A former owner loses management authority immediately. They retain reader access only if explicitly present in the reader ACL. A disabled owner is shown as unavailable to managers; an administrator can recover ownership. |
| H5 | CLI entry-review mutations require `EKI_API_TOKEN` and the same identity/account-status checks as HTTP; capture retains its expert checks, while maintenance requires the current owner subject. `--review-as NAME` cannot grant authority. This intentionally replaces token-free CLI review; read-only local inspection and trusted operator account/transfer commands retain their current model. |
| H6 | New review decisions store the authenticated subject and a display-name snapshot. Preserve legacy name-only decisions without inferring an identity from a name. Transferring ownership never rewrites approval or source authorship. |
| H7 | Successor entry edits create immutable, attributed maintainer turns (`speaker=maintainer`, `kind=edit`, author subject/name) in the entry's session/topic. They cannot edit or impersonate an original expert turn. Existing interview-turn citations can reference these edits; source validation accepts only public, correctly scoped human turns and verifies maintainer provenance at creation. A later owner change does not invalidate an earlier authorized maintainer's evidence. |
| H8 | No model/tool acquires approval or transfer powers. Reader access still exposes approved entries and safe citation metadata only, without confidential text or transcript expansion. Capture writes are refused after maintenance starts. |

### 5.2 Entry review and freshness

| ID | Requirement |
| --- | --- |
| D1 | Every approved entry has `review_by`, `last_reviewed_at`, `last_reviewed_by_subject` (nullable for legacy provenance), a reviewer name snapshot and `reviewed_version`. New approvals validate complete sections and citations before setting these fields. |
| D2 | An explicit “Reviewed; no changes needed” action validates the current approved version and its sources, appends a review decision and renews the date without editing claims or original approval metadata. It does not require a model. |
| D3 | Keep content/publication `version` separate from a monotonically increasing `maintenance_revision`. Review/date and flag metadata updates do not trigger embeddings or invalidate citations; all read surfaces obtain this metadata from live rows. Content edits still return entries to draft, increment their existing version and invalidate their index atomically. |
| D4 | Compute freshness at read time with an injected UTC clock. No scheduled status changes are required. Handover, flag submission and flag dismissal do not extend review dates. |
| D5 | Search, entry detail, Q&A evidence and Markdown show the current owner where appropriate, last review, review-by date, freshness, and an open-feedback signal. They distinguish original approval from later reviews. Never expose flag descriptions, reporter identities or management history to other readers. |
| D6 | Q&A excludes overdue entries in both retrieval channels and rechecks freshness, approval, version and ACL after provider latency. A date crossing midnight must not return newly overdue evidence. Search still returns overdue entries with labels; draft/rejected entries remain absent everywhere readers use knowledge. |
| D7 | The owner can withdraw an approved entry to draft with a redacted reason, preserving its history and invalidating the published index atomically. Re-publication requires explicit approval and a new review date. |

### 5.3 Flags and the maintenance queue

| ID | Requirement |
| --- | --- |
| R1 | Flag creation checks live read access and an expected approved entry version inside the transaction. Stale submissions return 409 with a prompt to reopen the entry, without silently attaching the report to a different version. Inaccessible entries return 404. |
| R2 | The flag body is redacted before storage or display and rendered as plain text. Flags are untrusted reader input, never model instructions, never automatically cited facts and never included in model prompts. |
| R3 | A client-generated UUID makes retries idempotent per reporter and entry. Replaying the same normalized request returns the original flag; changing the request under the same ID returns 409. Enforce an authenticated limit of 10 new flags per minute per subject, shared across API workers; retries do not consume new submissions. |
| R4 | Owners list and resolve all flags for their run. A reader sees their own receipts and resolution code/time while they retain run readership, including while the entry is temporarily draft; the receipt contains no draft content, new private version details, owner-only resolution notes or other reporters' data. Revoked readers receive 404. |
| R5 | Authorized readers see only the aggregate open-feedback signal on a currently approved entry. A flag cannot broaden any entry/run ACL. The current owner inherits the queue on transfer; flag authors retain their original attribution. |
| R6 | Resolution is checked against the current owner, expected maintenance revision and relevant entry version. “Corrected” and “confirmed current” enforce the actions in section 4.3. The public receipt contains only the resolution code and time; explanatory notes remain owner/admin-only. |
| R7 | A paginated queue, filtered by run and allowed management scope, supports `overdue`, `due_soon` and `flagged`; filters can overlap. Order overdue entries by earliest review date, then open flags by age, with stable entry-ID tie breaks. Show owner, status, observed/current versions and counts without fetching an entire transcript. |

### 5.4 Transactions, privacy and compatibility

| ID | Requirement |
| --- | --- |
| C1 | Use a documented lock order of run, session, entry and flag rows wherever those rows are acquired together. Align existing review/correction/synthesis paths as needed. A transfer racing an old owner's write must yield either an authorized write committed before transfer or a refused stale write afterward. |
| C2 | Review, withdrawal and flag resolution require expected entry version and maintenance revision. A stale browser must not approve content or overwrite triage it did not inspect. Validate again under lock and return 409 on a mismatch. |
| C3 | Persist state and metadata-only audit events in the same transaction. Extend the audit allowlist for handover, maintenance review, withdrawal and flag actions. Audit records actor/target IDs, action, versions and counts; reasons, flag bodies, prompts, credentials and confidential text stay out of audit/traces/logs. Application history holds redacted human reasons under its own ACL. |
| C4 | Preserve the seven sections, source citations, unsupported-claim advisory, redaction and approval gate. Maintainer-source support must extend validators, serializers, citation labels, faithfulness helpers and tests; it must not weaken cross-topic, confidential or corrected-source rejection. |
| C5 | Reader-visible metadata and counts are filtered by live ACLs before aggregation. No cross-run counts, hidden titles or revoked reporter receipts may leak through queue/search/answer endpoints. Cookie mutations retain Origin/CSRF enforcement; bearer and account disablement rules apply normally. |
| C6 | Additive migrations backfill legacy approved entries with `last_reviewed_at=approved_at`, their existing reviewer name and `review_by=UTC date(approved_at)+90 days`. Leave reviewer subject null; do not infer identity. Older approvals may become overdue immediately and therefore ineligible for Q&A. Preserve existing publication status, bodies, citations and decision history. |
| C7 | Legacy runs stay in capture until an explicit handover, including unassigned runs. An administrator can transfer a legacy run directly into maintenance without locating the departed expert or first repairing old display-name bindings. |

## 6. API and CLI proposal

These are proposed contracts. Request models remain strict, validate bounds,
and derive the actor from authenticated context rather than a caller-supplied
reviewer name. Existing review endpoints gain maintenance authorization and
version checks; compatibility handling must never authorize by name after handover.

| Surface | Proposed operation |
| --- | --- |
| `POST /runs/{id}/handover` | Admin: `{owner_subject, expected_owner_subject, expected_run_revision, reason}`; enter maintenance and return its new state. `null` expected owner explicitly means unassigned. |
| Existing owner update routes/CLI | Remain admin/operator-only; share validation/history and expected-state checks. In maintenance, changing the owner also changes review authority. |
| `GET /runs/{id}/maintenance` | Owner/admin: mode, current owner, handover history and queue summary; no credentials. |
| `GET /runs/{id}/maintenance/entries` | Owner/admin: paginated queue with freshness/flag filters and stable ordering. |
| Existing `POST /entries/{id}/approve` | Add `{expected_version, expected_maintenance_revision, review_by?}`; maintenance identity comes from the principal. |
| Existing entry edit/reject routes | Add expected-version checks; maintenance service checks the current owner and records attributed edits/decisions. |
| `POST /entries/{id}/review` | Current review authority: confirm unchanged approved content, renew the date and record the reviewed version. |
| `POST /entries/{id}/withdraw` | Current review authority: return approved content to draft with a reason and expected versions. |
| `POST /entries/{id}/flags` | Reader: `{request_id, expected_version, category, section?, description}`; 201 created, 200 identical replay. |
| `GET /entries/{id}/flags` | Owner/admin sees paginated triage data. Readers use their own flag receipt endpoint. |
| `GET /flags/{flag_id}` | Owner/admin sees scoped triage data; the reporter sees only their permitted receipt under live run readership, even while entry content is draft. |
| `POST /entries/{id}/flags/{flag_id}/resolve` | Current owner: resolution, private redacted explanation, expected versions and reviewed/replacement version as applicable. |

CLI additions: `eki handover --run ID --to SUBJECT --expected-owner SUBJECT
--reason TEXT`, `eki maintenance --run ID --filter overdue`, and
`eki review --run ID --due` for queued review. Existing `eki review` gains
review-date, unchanged-review, withdrawal and flag-triage actions. The unassigned
handover case uses an explicit `--unassigned` alternative. Ownership transfer
keeps the trusted local operator model; review mutations now require a configured
credential satisfying the capture or maintenance authority rules. No command needs model configuration for these actions.

## 7. Browser workflow

- The run header distinguishes “Expert: Nadia Ferrand” from “Maintained by:
  Samir”. A maintenance handover never relabels the historical interview.
- An admin handover dialog shows the current and destination owner, existing
  drafts/flags, the capture-write closure and a required reason before transfer.
- A **Maintenance** view gives the owner due-soon, overdue and flagged filters.
  It links to the existing entry review view with version/revision information.
- Entry review shows last review, deadline, attribution and open flags. Actions
  are **Edit**, **Reviewed; no changes needed**, **Withdraw** and flag triage.
  Draft approval includes a proposed next review date. Content edits retain the
  current explicit save and approval steps.
- Readers see freshness/feedback badges and **Report a problem** on an approved
  entry. The form explains that a report reaches the maintainer and does not
  edit the procedure. Success returns a durable receipt; a retry reuses its ID.
- Search results, answers and exports carry the same freshness labels. Empty
  answers caused by overdue evidence explain that a review is needed, using
  only data the reader may see. Raw reports are not shown alongside knowledge.
- Browser tests cover keyboard use, focus/error recovery, conflicting updates,
  narrow layouts and accessible labels. UI copy must say “review overdue”,
  rather than claiming an expired date proves the procedure is false.

## 8. Data and code impact

Proposed schema additions, numbered after the migrations present when work
starts:

- Run maintenance mode/start time, a run maintenance revision for transfer CAS,
  and handover history referencing the existing owner subject.
- Entry review fields and an entry maintenance revision distinct from its
  existing content/publication version. Review history includes actor subject,
  name snapshot, reviewed version, deadline and time. Run and entry revision
  counters are separate and named explicitly in request/response models.
- Entry flags with reporter subject, observed version, request ID, category,
  section, redacted description, state and immutable resolution attribution.
- An attributed maintainer turn variant plus subject/name fields; legacy expert
  turns are retained as recorded. New maintainer turns can only be created by
  the authorized maintenance edit service, not by public transcript APIs.
- Durable flag throttling state, using the existing account-throttle pattern
  where suitable without mixing authentication counters with content reports.

Primary code areas: `eki.access`, `eki.runbook` review/service/store/models,
`eki.interview` turn storage/validation, `eki.api` security/routes/serialization,
`eki.search` query/evidence checks, Markdown export, audit models, CLI and the
React workspace. Keep authorization and maintenance transitions in shared
service functions; browser or route-only checks do not satisfy this PDR.

## 9. Acceptance criteria

| # | Criterion |
| --- | --- |
| A1 | With the original expert disabled, an admin hands a synthetic run to a differently named successor. The successor edits an entry, approves the complete cited draft, and a reader can retrieve/export the new version. No original expert credential is needed. |
| A2 | A former owner, reader, admin who is not the owner, and a different subject sharing the successor's name cannot approve/edit/renew/resolve in maintenance. Test HTTP, service and CLI paths, including existing ownership commands and account disablement. |
| A3 | Original answers/approvals retain their attribution. A successor edit cites a maintainer turn showing the actual subject/name; a second successor can review that evidence after another transfer. Confidential, cross-topic and corrected-source claims still fail validation. |
| A4 | Handover alone changes neither entry status nor review deadline, publishes no draft, preserves open flags and closes capture writes. An accepted pending message prevents handover until settled; transfer/account disablement races have deterministic outcomes. |
| A5 | At frozen UTC boundaries, an entry is current, due soon and overdue on the defined dates. It becomes overdue at midnight without any scheduled job or database write. Approval/unchanged review validate date bounds and record the reviewed version. |
| A6 | Unchanged review renews the date with subject/history/audit, preserving original approval, claims, citations and content version. A date-only bypass is refused; stale expected versions return 409. |
| A7 | Overdue approved entries remain labelled in detail/search/Markdown and cannot enter Q&A context through either channel. An entry becoming overdue or losing approval/access during provider latency suppresses the answer. Counts/explanations leak no inaccessible data. |
| A8 | A reader flags an accessible approved version; text is redacted and rendered safely. Identical retries create one flag; changed retries and stale versions conflict. Shared-worker rate limits and invalid/oversized payloads are covered. |
| A9 | Owners triage flags; readers see only their own safe receipts, even while an entry is draft. Another reader's identity/description and private resolution notes never appear. Revocation blocks receipt access. A flag never directly edits, withdraws or publishes an entry. |
| A10 | Corrected/confirmed-current resolutions require their specified approval/review; dismissal needs a reason and never renews a date. Unresolved flags survive edits, new versions and transfers. Withdrawal removes the entry from reader retrieval/export until reapproval. |
| A11 | Interleaved transfer/review/edit/resolve requests respect the shared lock order and expected revisions, with no unauthorized commits, duplicate effects, stale approval or deadlock. Audit failure rolls back the corresponding change. |
| A12 | A legacy database migrates without changing original content/status/citations/actors. Dates backfill from actual approval time; legacy subjects stay unknown. Unassigned and departed-expert runs can enter maintenance. Document and test the immediate overdue/Q&A impact. |
| A13 | Browser owner/admin/reader journeys, keyboard and responsive accessibility checks pass. The existing unit, database, red-team and deterministic eval suites, Ruff, mypy, frontend typecheck/tests/build and relevant browser checks pass. No paid models are required. |

## 10. Deliverables and order

1. **8a — Successor authority and provenance.** Run handover/history, shared
   owner checks, service/API/CLI authorization, capture closure and attributed
   maintainer edits/citations. Database and adversarial authority tests (H1–H8,
   C1/C3/C4/C7, A1–A4/A11).
2. **8b — Review lifecycle and read policy.** Dates/backfill, unchanged review,
   withdrawal, version checks, queue foundations and consistent search/Q&A/export
   freshness. Frozen-clock and concurrent-read tests (D1–D7, C2/C5/C6, A5–A7/A12).
3. **8c — Reader flags and triage.** Durable/idempotent reports, shared throttling,
   scoped queue, receipts/resolution and audit (R1–R7, A8–A11).
4. **8d — Browser and operational documentation.** Handover, maintenance queue,
   review/flag forms, badges, updated tutorial/flows/PDR/UI definition and all
   acceptance evidence (A13). Record verification and remaining manual work in
   an Outcome section; do not label the feature implemented before this work.

Use F21 in deliverable commits. Keep schema changes backward readable during
rollout where practical; deploy validators, read metadata and the Q&A freshness
policy together so an old client cannot hide overdue state or bypass review
rules. Explain the legacy-date/Q&A change in operator release instructions.

## 11. Decisions to confirm when approving this PDR

The proposed defaults make the plan implementable without inventing policy
later. Approval should explicitly cover:

- A run-level owner, admin/operator-controlled transfer and explicit closure of
  capture work on entering maintenance.
- Review due in 90 days by default, a 14-day due-soon window, dates bounded to
  the next 365 days, and legacy deadlines derived from original approval time.
- Overdue approved entries remaining searchable/readable/exportable with labels,
  while Q&A excludes them; reader flags alone do not suppress knowledge.
- Private reader reports/resolution notes, safe own-report receipts and explicit
  owner triage rather than automatic closure or publishing reader comments.
- Authenticated CLI review mutations, replacing the current token-free
  `eki review` behaviour while keeping trusted operator provisioning/transfer.

Implementation approval and any later changes to these decisions will be
recorded in Status. No runtime code, migration, account or ownership change is
part of creating this proposal.
