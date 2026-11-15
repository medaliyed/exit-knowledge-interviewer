# F16: prototype-based browser workspace

Scope authorized by the owner's request to implement the supplied
`eki workspace.html` and `docs/ui/ui-pdr.md`, following the reviewed stack.
This deliverable refines the browser workspace; ingestion, mapping, ownership,
real-provider validation and external publishing remain outside its scope.

## Delivered

- React/TypeScript/Vite frontend, same-origin production assets included in the
  Python package, and browser routes under `/workspace/*`.
- Prototype colors, typography, components and responsive layouts using Tailwind
  and locally owned shadcn-style Radix controls. System fonts and bundled assets.
- Token-only sign-in; per-handover owner/reader navigation; plan drafting/resume,
  source-grounded plan review, drag and keyboard reordering, explicit approval.
- Interview checklist, transcript, skip/confidential/correction controls,
  streamed notes, stable message identity and receipt-based recovery.
- Conversation-scoped review, source expansion, section edits, required rejection
  reasons, decision history and explicit reapproval after edits/corrections.
- Approved-only search/Q&A with individual evidence references and Markdown
  downloads. Search scope is labelled separately from selected-handover listings.
- Expert-name validation for HTTP plan approval, matching the PDR and entry review.
- Pinned frontend dependencies, lockfile, build/test documentation and CI job.

## Verified

- 430 provider-free Python unit tests and 222 disposable-schema database tests.
- Ruff checks/formatting and Mypy.
- TypeScript production build, 8 new transport tests and 4 retained client tests.
- 22 Playwright workflows with a stateful synthetic API double, also run against
  production files served by FastAPI's strict CSP with no policy violations.
- Automated accessibility/overflow checks at 360, 768 and 1440 px; keyboard topic
  reordering and evidence-to-section focus; reviewed representative screenshots.
- Python source/wheel builds include the frontend HTML, JavaScript and CSS.

## Review follow-up (15 Nov 2026)

A walkthrough of the first build found these gaps; each fix landed with a
browser test that failed first (`frontend/e2e/review-fixes.spec.ts`):

- An unsent interview answer now triggers the browser's leave-page warning on
  refresh or close, and a confirmation dialog on sign out. Drafts still live only
  in tab memory.
- Success notices clear themselves after six seconds and sit top-right, clear of
  the composer buttons. Error notices stay until dismissed.
- Removing a topic asks for confirmation that names the topic and its drafted
  questions.
- Entry pages highlight the tab they belong to; tabs wrap instead of clipping on
  phones; the review page no longer shows internal session identifiers; the
  transcript column explains itself before the first answer; streamed
  "Noted" feedback has a designed treatment.

Live-provider behavior and a human screen-reader walkthrough remain pending.
The browser double verifies UI behavior; Python HTTP/database tests verify the
backend contracts. Neither substitutes for real-provider acceptance validation.
