# Phase 3 (Runbook), first half: Implementation Plan

> **For agentic workers:** implement task by task, in order a to h, test first, one commit per task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** approved by the user on 2026-11-14; implementation in progress.

**Scope of this half:** F9 (runbook entries with citations) and F10 (approve, edit, reject). **Left for the second half:** F11 (hybrid search and Q&A with access filtering) and F12 (Markdown export). Nothing in this half is searchable or exported; it ends with approved entries stored in Postgres and readable through the API and the command line.

**Goal:** When an interview finishes, turn each topic's notes and open items into one draft runbook entry in the seven-section template, where every claim cites the interview turn it came from. The expert then approves, edits or rejects each entry. Nothing counts as approved without that decision.

**Architecture:** One new fixed node, `synthesise`, at the end of the existing workflow. It is plain code: no model call. Approval, editing and rejection are ordinary operations on stored entries, outside the workflow, because each is one atomic database transaction with nothing to resume. The entry and citation models from phase 0 (`eki.runbook.models`) are used as they are.

**Tech stack added:** none. No new dependency.

**Spec:** `docs/PDR.md` (F9, F10), `CLAUDE.md` (hard rules), `docs/plans/phase-2.md` (notes, open items, confidential answers).

## What this half does and does not prove

| Claim | How it is checked |
| --- | --- |
| Every claim in an entry cites an expert turn that exists and belongs to that topic | Automated, on the fixture interview. Needs no model and no key. |
| An uncited claim blocks approval | Automated: already enforced by the entry model; tested again through every path that can change an entry. |
| A confidential answer never appears in an entry | Automated: the fixture interview includes one, and every stored entry is scanned. |
| Nothing is approved without the expert's decision | Automated: synthesis only produces drafts; the only way to `approved` is the approve operation. |
| The entries read well | **Not checked.** Entries carry the expert's words as recorded in the notes. Their quality depends on the notes, and so on a real model in phase 2, which has still never run. |

## Decisions that need your answer

Approving the plan as written accepts the recommended option for each.

| # | Decision | Recommended | Alternative |
| --- | --- | --- | --- |
| D1 | Who writes the entry text. | **Code, not a model.** Each note becomes one claim with the note's text. A `steps` note is split into one claim per sentence, in order. The title is the topic label. Deterministic, testable without a key, and it cannot invent anything. | A model rewrites the notes into polished claims, with a check that each claim's words are supported by the turn it cites. Reads better, but adds a way to introduce unsupported text and cannot be verified without a key. Can be added later behind the same interface. |
| D2 | What a claim cites. | The interview turn its note came from (session and turn number). | Also cite the artifact that prompted the question. Rejected as the default: that artifact usually shows the gap, not the answer, so citing it for the claim would mislead. |
| D3 | Sections without a note. | An open item becomes the section's open question, with the expert's reason. A confidential answer becomes the open question "Answered confidentially; not published." No section is left empty and nothing is guessed. | Leave confidential sections empty, which would make the template incomplete. |
| D4 | How an edit stays cited. | An edit by the expert is stored as a new expert turn in the transcript (kind `edit`), and the edited claims cite that turn. So F9 holds for edited text too. Edited text is redacted like any answer. | Allow uncited edits and block approval until someone adds a citation by hand. |
| D5 | What happens to an approved entry when its source changes. | If the expert corrects a turn that an approved entry cites, or edits the entry, it goes back to `draft` and must be approved again. A rejected entry that is edited also returns to `draft`. | Keep the approval and show a warning. |
| D6 | Who may approve. | The name given must match the expert of the run (`ingest_runs.expert_name`). This is a consistency check, not security: there is still no authentication. Real identity arrives with F11 in the second half. | Accept any non-empty name, as plan approval does today. |
| D7 | Entries where everything is open (the "I don't remember" topic). | They can be approved. "Nobody knows this" is worth publishing. The expert can also reject them. | Refuse approval of an entry with no claims. |
| D8 | When synthesis runs. | Automatically when the last topic finishes, and again on request (`POST /sessions/{id}/synthesise`). Re-running replaces drafts only; approved and rejected entries are left alone unless D5 applies. | On request only. |
| D9 | Rejection. | Needs a reason, which is stored with who and when. | No reason required. |

## Global constraints

- Nothing is indexed, logged or sent to a model before redaction. This half sends nothing to a model.
- A confidential answer never goes into an entry, an API response or a log.
- Only approved entries may ever be published. This half publishes nothing; it must not create a path that would.
- Never edit `evals/scenarios/` or `evals/golden/`. The pipeline never reads `answer_key.json` or `expert_answers.json`.
- No new dependency. No secrets in code. Config through `eki.config`.
- Test first. One requirement per commit, with the requirement ID in the message: `<type>(<ID>): <summary>`, `<ID>` is `F9`, `F10` or `P3-<letter>`.
- After every task run and show: `uv run pytest -q`, `uv run pytest -q -m db`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`.

## Review focus

1. **A note is corrected after its entry was approved.** Expected: the entry returns to draft. Test `test_correction_reopens_an_approved_entry` in task e.
2. **Two decisions on the same entry at once.** Expected: the row is locked; the second sees the new state and is refused if it no longer applies. Test `test_decisions_are_serialised` in task d.
3. **An edit that removes every claim from a section.** Expected: refused unless the section gets an open question, so the template stays complete. Test `test_edit_cannot_leave_a_section_empty` in task e.
4. **Synthesis is run twice.** Expected: same drafts, no duplicates, approved entries untouched. Test `test_synthesis_is_repeatable` in task b.
5. **A secret typed into an edit or a rejection reason.** Expected: redacted before storage. Test `test_edit_and_reason_are_redacted` in task e.
6. **The interview is not finished.** Expected: synthesis on request is refused. Test `test_unfinished_session_cannot_be_synthesised` in task c.

## File structure

```
src/eki/runbook/synthesis.py     build_entry(): notes and open items of one topic -> draft RunbookEntry
src/eki/runbook/store.py         entries and decisions in Postgres
src/eki/runbook/review.py        approve, reject, edit: the rules, as pure functions on an entry
src/eki/runbook/service.py       operations shared by the API and the command line
src/eki/graph/workflow.py        new fixed node `synthesise` after the last topic
src/eki/migrations/0003_runbook.sql
src/eki/api/app.py               entry routes
src/eki/cli.py                   eki entries, eki review
tests/...                        one test module per source module, plus tests/test_phase3a_acceptance.py
```

---

### Task a: Entry storage

**Files:** `src/eki/migrations/0003_runbook.sql`, `src/eki/runbook/store.py`, `tests/test_runbook_store.py` (`db`).

Tables: `runbook_entries` (ID, session, topic, title, status, content as validated JSON, approver, approval time, rejection reason, version) with one entry per session and topic; `entry_decisions` (entry, action, who, when, reason) as the history. The `turns.kind` check gains `edit`.

- [x] Failing tests: an entry round-trips unchanged; one entry per session and topic; an approved entry with an uncited claim cannot be stored; deleting a run removes its entries and decisions; decisions are returned in order.
- [x] Implement. Commit `feat(P3-a): runbook entry storage`.

### Task b: Synthesis (F9)

**Files:** `src/eki/runbook/synthesis.py`, `tests/test_synthesis.py`.

**Produces:** `build_entry(session_id, topic, notes, open_items) -> RunbookEntry`, a pure function.

- [x] Failing tests: each note becomes a claim citing its turn; a `steps` note is split into ordered claims that all cite the same turn; an open item becomes the open question with the expert's reason; a confidential note becomes the fixed open question and its text appears nowhere; superseded notes are ignored; the result is a draft with no missing section and no uncited claim; `test_synthesis_is_repeatable`; a topic with no notes and no open items is refused.
- [x] Implement. Commit `feat(F9): build cited runbook entries from interview notes`.

### Task c: Synthesis in the workflow

**Files:** `src/eki/graph/workflow.py`, `src/eki/runbook/service.py`, `tests/test_workflow_synthesis.py` (`db`).

- [x] Failing tests: when the last topic finishes, one draft entry exists per plan topic; the session still ends as finished; a restart during synthesis leaves no duplicate; synthesis on request replaces drafts and keeps approved and rejected entries; `test_unfinished_session_cannot_be_synthesised`; the existing phase 2 workflow tests still pass.
- [x] Implement. Commit `feat(F9): synthesise draft entries when the interview ends`.

### Task d: Approve and reject (F10)

**Files:** `src/eki/runbook/review.py`, `src/eki/runbook/service.py`, `tests/test_review.py`, `tests/test_review_service.py` (`db`).

- [x] Failing tests: approve sets status, approver and time and records a decision; an approver who is not the run's expert is refused; an entry with an uncited claim or a missing section cannot be approved; approving twice is refused; reject needs a reason and records it; a rejected entry cannot be approved without an edit; an all-open entry can be approved; `test_decisions_are_serialised`; unknown entry gives not-found.
- [x] Implement. Commit `feat(F10): expert approves or rejects each runbook entry`.

### Task e: Edit and re-approval (F10)

**Files:** `src/eki/runbook/review.py`, `src/eki/runbook/service.py`, `src/eki/interview/service.py`, `tests/test_entry_edit.py` (`db`).

An edit replaces the claims or the open question of one or more sections. It is stored as an expert turn of kind `edit`, and the new claims cite it.

- [x] Failing tests: edited claims cite the new turn and the turn is in the transcript; `test_edit_cannot_leave_a_section_empty`; an edit to an approved or rejected entry returns it to draft; `test_correction_reopens_an_approved_entry`; a correction that touches no cited turn leaves the approval; `test_edit_and_reason_are_redacted`; the version number rises with each change.
- [x] Implement. Commit `feat(F10): expert edits an entry; changes require re-approval`.

### Task f: API

**Files:** `src/eki/api/app.py`, `tests/test_api_entries.py` (`db`).

| Endpoint | Purpose |
| --- | --- |
| `POST /sessions/{session_id}/synthesise` | Rebuild draft entries from the current notes |
| `GET /sessions/{session_id}/entries` | List entries with status and counts of claims and open questions |
| `GET /entries/{entry_id}` | One entry with its claims, citations and decision history |
| `PATCH /entries/{entry_id}` | Edit sections |
| `POST /entries/{entry_id}/approve` | Approve |
| `POST /entries/{entry_id}/reject` | Reject with a reason |

- [x] Failing tests: the full flow from a finished interview to an approved entry; 404 for unknown IDs; 409 for a decision that no longer applies and for an unfinished session; 422 for a bad edit, a missing reason and a wrong approver; no response contains a planted secret or a confidential answer; the route list test is updated.
- [x] Implement. Commit `feat(P3-f): runbook entry review API`.

### Task g: Command line

**Files:** `src/eki/cli.py`, `tests/test_cli.py` (`db`).

- [x] `eki entries --plan ID`: list the entries of the plan's session with their status. `eki review --plan ID`: show each draft entry with its citations and ask approve, reject (with a reason), edit a section, or skip; it can be quit and resumed.
- [x] Failing tests with scripted input: list, approve one, reject one with a reason, edit one then approve it, quit and resume.
- [x] Implement. Commit `feat(P3-g): eki entries and eki review`.

### Task h: Acceptance and docs

**Files:** `tests/test_phase3a_acceptance.py` (`db`), `CLAUDE.md`, this file (Outcome).

- [ ] On the fixture interview from phase 2 (scripted fakes and the simulated expert, with one confidential answer added): one draft per topic; the G3 entry has seven sections of cited claims and its rollback claim cites the follow-up turn; the G7 entry is all open questions in her words and has no claim; every citation points at an existing expert turn of the same topic; no entry holds a planted secret or the confidential answer; approving G3 and rejecting a filler entry leaves the rest as drafts.
- [ ] Five verification commands and the `infra` tests. Commit `feat(P3-h): phase 3 first-half acceptance tests`.

---

## Closing report

List what is verified with evidence, a sample entry, and what is not verified.

## Out of scope for this half

Search, Q&A, access control and authentication (F11). Markdown export (F12). Model-written entry text (D1 alternative). Mail, red-team suite, evals in CI, audit log (phase 4). A web UI.
