# Prompt for Claude Design

Paste everything between the two lines into Claude Design. It is self-contained:
Claude Design cannot see this repository. The full requirements are in
[ui-pdr.md](ui-pdr.md); keep the two in step when either changes.

Example content in the prompt is synthetic (the project's made-up expert,
Nadia Ferrand). Never paste real names, tickets or answers into a design tool.

---

Design the browser workspace for **eki (Exit-Knowledge Interviewer)**: a tool
that interviews an expert before they leave a company and turns their answers
into a cited runbook their team can trust.

## The product in one paragraph

Before an engineer retires or leaves, eki reads their tickets, code history and
selected emails, finds the topics only they know, and drafts an interview plan.
An AI interviewer then asks the expert one question at a time until each topic
is covered. Every answer becomes a runbook entry with seven sections, and every
statement in it cites the answer it came from. Nothing is published until the
expert approves it. Colleagues can then search the approved runbook and ask
questions; answers are made of the expert's exact approved words, with sources.

## Who uses it

1. **The expert** (primary): senior, often not technical, short of time,
   leaving. Uses it over several sittings. Needs calm, clarity, and confidence
   that nothing they say is lost or published without them.
2. **The reader**: a colleague or successor. Only searches, asks and reads
   approved knowledge. Sees nothing else: no drafts, no interview, no review.

## Principles

- One thing at a time: one question, one entry.
- Show the source: every statement shows where it came from; every drafted
  question shows the exact quote that prompted it.
- The expert decides, visibly: approve, reject and edit are explicit actions.
- Never lose an answer, and say so after every submission.
- Gaps are honest states: "not answered", "skipped", "confidential", "not
  covered yet" each get a designed treatment.
- Warm, plain, unhurried tone. No gamification, no confetti, no streaks.

## Screens to design

### 1. Sign in
One field for an access token, one button "Open workspace". A note: the token
stays in this tab only; after a refresh you sign in again. Welcome copy to keep:
"A thoughtful handover. A lasting reference." and three steps: *Make a plan*,
*Have the conversation*, *Make it yours*.

### 2. Workspace shell
Header with "eki" wordmark, signed-in name, Sign out. Sidebar list of
**handovers** (one per expert and period, e.g. "Nadia Ferrand · Nov 2024 – Oct
2026"). Main area with tabs for the selected handover:
**Interview plans · Review entries · Approved knowledge**. Readers see only
Approved knowledge.

### 3. Interview plans
List of plans with status: drafting, ready for review, interview in progress,
interview finished. Button "Draft a new plan". Drafting takes minutes (an AI
drafts questions topic by topic): show progress "3 of 10 topics drafted", and a
failure state "7 of 10 drafted. Resume to continue" with a Resume button.

### 4. Plan review
Ordered list of topics. Each topic card shows:
- title, e.g. "Clarify invoice rounding for multi-currency tenants";
- kind and system, e.g. "procedure · billing-batch";
- why it is in the plan: a risk score 0–100 ("86.8: only Nadia worked on this,
  nothing is documented") or "Sources contradict each other";
- drafted questions, each with its source quote, e.g.
  Q: "TCK-0026 says *replicas first, then the billing-batch workers*, but
  TCK-0075 says *restart the billing-batch workers first*. Which is right?"
  from TCK-0026: "replicas first, then the billing-batch workers".
Actions: reorder (drag, plus keyboard up/down), remove topic, add a topic (name
plus a note in the expert's words), and **Approve plan** with a confirmation
("Approving starts the interview. The plan can't be changed afterwards.").
Contradiction topics should stand out: they are asked first.

### 5. Interview (most important screen)
- Centre: the current question, large and readable, and a multi-line answer box
  under it.
- Around it, quieter: topic name and "Topic 2 of 4"; a checklist of the seven
  sections of the current topic with a status each: **missing, noted, left
  open, confidential**. Sections: trigger, diagnosis, steps, rollback,
  contacts, pitfalls, rationale.
- Transcript of the conversation so far, collapsible.
- Controls: **Send**; a **Confidential** toggle with a clear explanation (stored
  but never sent to the AI, never shown to readers, the runbook says only
  "answered confidentially"); **Skip this question**; **Correct an earlier
  answer** (pick it from the transcript; show old and new text); leaving is
  always safe, say so.
- After sending, stream what the interviewer did, e.g.
  "Noted · steps: Open the tenant's rounding profile. Set the mode to
  half-even. Re-run billing-batch for that tenant only." or
  "Left open · rollback: skipped by expert".
- States to design:
  - thinking (the AI can take 5–30 seconds);
  - "Your answer is saved" after every send;
  - interrupted: "Your answer is saved. Continue" (the server kept it, the
    next question didn't arrive);
  - AI unreachable: "Nothing was lost. Try again.";
  - confidential answer in the transcript shows "[confidential]", never text;
  - topic finished → next topic;
  - interview finished: "26 sections noted, 2 left open" and a button to review.

Example exchange:
> **Q** (turn 1 · trigger): What is behind TCK-0120?
> **A**: Finance reports that a multi-currency invoice total is off by one cent.
> Noted · trigger
> **Q** (turn 3 · diagnosis): You did not mention the diagnosis. What is it?
> **A**: I don't remember exactly how I narrowed it down.
> Left open · diagnosis: "I don't remember exactly how I narrowed it down."

### 6. Review entries
List of entries: title, status (draft / approved / rejected), badges "edited by
hand" and "2 claims to check", version.
Entry view: seven sections in order. Each section shows its statements
("claims") or an open question. Steps are numbered. Every claim shows its
source, e.g. "interview · turn 6", expandable to read that answer. A claim the
system flags shows a soft warning under it: "Not in the cited answer: 15,
minutes". It is a hint to check the wording, not an error.
Actions: **Approve**, **Reject** (reason required), **Edit** (per section:
claims, or an open question if there are none), **History** of decisions.
States: a confidential section ("Answered confidentially; not published"), a
skipped section ("Open: skipped by expert"), an entry reopened because its
source answer was corrected.

Example entry "report-export":
- Trigger: The nightly export finishes but the report file is empty. *(turn 17)*
- Diagnosis: Open: "I don't remember exactly how I narrowed it down."
- Steps: 1. Check the export queue depth. 2. Restart the export worker.
  3. Re-queue the report. *(turn 21)*
- Rollback: Open: skipped by expert
- Contacts: Answered confidentially; not published.
- Pitfalls: Restarting during a running export duplicates rows. *(turn 27)*
- Rationale: The worker leaks connections after about a week. *(turn 29)*

### 7. Approved knowledge (owner and reader)
- **Search** box; results: entry title, the matched section, a snippet, sources.
- **Ask** box; the answer is a list of the expert's exact approved statements,
  each with a numbered evidence tag, and a legend under it (entry, section,
  version). Procedures come back whole and in order. No-answer state: "The
  approved runbook doesn't cover this yet. Try searching for key words."
- **Read entry**: approved by, date, version, sources as references.
- **Download** an entry or the whole handover as Markdown.

Example answer to "In what order do I restart after a failed posting phase?":
1. Restart the ledger-db replicas. [E2]
2. Wait until replica lag is zero. [E6]
3. Restart the billing-batch workers. [E1]
4. Re-run the posting phase. [E4]
Legend: E1–E6 · "billing-batch run failed during the posting phase", steps, v2.

## Visual direction

Refine the existing identity rather than replacing it:
- Palette: paper `#f7f7f2` background, ink `#243b39` text, muted `#687875`,
  lines `#dce3dc`, accent green `#176e5b`, pale green `#e7efe7`, warning rust
  `#9c4b2d`, focus ring gold `#b48c36`.
- Mood: a well-made notebook or archive: quiet, paper-like, generous spacing,
  serious but kind. Not a chat app clone, not a dashboard.
- Typography: system font stack only (no web fonts). Clear hierarchy; large,
  comfortable reading size for questions and entries.
- Provenance styling: one consistent, unobtrusive way to show a source on every
  claim, used everywhere.
- Status needs text labels, not colour alone.

## Constraints the design must respect

- Built as plain HTML, CSS and JavaScript served from the same origin. No
  external fonts, scripts, icons or images from a CDN (strict content security
  policy). Icons: inline SVG or none.
- Fully usable at 360 px wide; desktop is the main target for review; the
  interview must work well on a tablet.
- WCAG 2.2 AA: keyboard access to every action (including reordering), visible
  focus, contrast checked, live announcements for streamed interview events.
- Nothing the user types appears in a URL. Search and ask are forms.
- All text from the AI and from source documents is shown as plain text.

## What to deliver

1. High-fidelity screens for 1–7 at desktop width (1440 px), plus the interview
   and an entry review at mobile width (390 px).
2. Every state listed above, especially the interview states and empty states.
3. A small component sheet: buttons, inputs, toggle, tabs, status badge,
   section checklist, claim with source, warning hint, topic card, evidence tag,
   notice/toast, confirmation dialog.
4. Design tokens: colours, type scale, spacing, radii, shadows, as CSS custom
   properties.
5. A short note on any place where you would change the flow, and why.

---
