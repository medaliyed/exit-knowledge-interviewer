# Evals

`evals/scenarios/` and `evals/golden/` are written and owned by humans.
Agents and automation must never create, edit or delete anything inside them.


Run automated synthetic contracts with:

```bash
uv run python evals/run.py --junit reports/quality.xml
```

The runner selects `eval` and `redteam` tests, excludes `live`/`infra`, propagates
failures to CI, and produces JUnit plus a GitHub job summary. It requires the
local Postgres service; `--unit-only` runs only contracts without a database.
The checked-in quality questions and assertions live in `tests/`. The runner
never creates, edits or deletes human-owned scenario/golden content.

Real-provider validation is currently deferred by user request. The checks
below are available for a future explicit opt-in; they are not needed for the
synthetic quality gate. Select the intended model roles, export credentials to
the test process and record an agreed call/token budget before paid checks.
A key present only in `.env` does not satisfy the live-test guard.

| Suite | Model roles/settings | What it checks |
| --- | --- | --- |
| `tests/test_live_model.py` | `EKI_MODEL_DRAFTER`, `EKI_MODEL_INTERVIEWER` | Follow-up on missing rollback, leaving unknown sections open and presenting contradictions |
| `tests/test_live_retrieval.py` | `EKI_MODEL_EMBEDDING` and/or `EKI_MODEL_ANSWERER` | Approved runbook paraphrases, cited evidence selection and abstention |
| `tests/test_live_artifacts.py` | `EKI_MODEL_ARTIFACT_EMBEDDING` | Source-artifact paraphrases with fixed expected IDs and distractors; three repetitions, separate hashing/semantic results |

Select the suite explicitly, for example:

```bash
uv run pytest -m live -s -rs tests/test_live_artifacts.py
```

Missing configuration skips with a reason before provider calls. A skip or a
passing scripted-provider test does not establish real-model quality. The
[Phase 5 live record](../docs/plans/phase-5-live.md) documents actual availability
checks and what remains pending.

## Golden interview scorecards (F20)

`grade.py` drives the real `InterviewService` against one planted topic per
scenario. It ingests the synthetic corpus through the read-only MCP servers,
using explicitly selected `Inbox`, `Sent` and `Vendors` mail folders, maps and
scores it, and saves only the chosen topic to a throwaway Postgres schema.
Normal redaction, audit, approval and confidentiality handling apply. The
schema is migrated and dropped for each scenario, even after a failure.

The eight proposed G1–G8 pairs live under
[`docs/plans/phase-7-drafts/`](../docs/plans/phase-7-drafts/README.md). They are
review drafts; agents must not adopt them into the human-owned directories.
Validate them offline:

```bash
uv run python evals/grade.py --dry-run \
  --scenarios docs/plans/phase-7-drafts/scenarios \
  --golden docs/plans/phase-7-drafts/golden
```

After the owner adopts pairs into the default directories, commands are:

```bash
uv run python evals/grade.py --dry-run
uv run python evals/grade.py --only G3-month-end --max-calls 25
uv run python evals/grade.py --compare reports/evals/PREVIOUS.json
```

To grade the review drafts directly, retain both directory flags and remove
`--dry-run`. A paid run needs `EKI_DATABASE_URL`, `EKI_MODEL_DRAFTER`,
`EKI_MODEL_INTERVIEWER` and a provider key exported to the process. A key in
`.env` alone does not satisfy the guard. Missing configuration stops before
any database or provider work. `--max-calls` defaults to 40 total provider calls
per scenario, shared by drafting and interviewing; the next call aborts that
scenario. The service's existing per-topic limits also apply. No paid run is
part of implementation verification.

### File format

Each scenario and golden is a JSON object with no extra keys. Both files are
named `<scenario-id>.json`; every scenario must have a golden and vice versa.
Unknown sections are errors that name the file. The seven sections are
`trigger`, `diagnosis`, `steps`, `rollback`, `contacts`, `pitfalls` and `rationale`.

| Scenario key | Meaning |
| --- | --- |
| `id`, `title` | Stable ID and human-readable purpose. |
| `corpus` | Existing synthetic corpus directory relative to the repository root. |
| `expert` | `name`, `email`, `work_domains` array. |
| `span` | Inclusive ISO dates `since` and `until`. |
| `topic_artifact_ids` | Nonempty array; select the smallest topic containing at least 80% of these IDs. Use source artifacts, not wiki document IDs. |
| `script.initial` | All seven section keys, each holding a string or null. The first answer volunteers nonempty values as labelled lines. |
| `script.follow_ups` | Section-to-string map for later answers; otherwise the initial text is repeated. |
| `script.freeform` | Optional string spoken on every turn instead of labelled answers. |
| `question_cap` | Hard question limit; default 30. |

| Golden key | Check |
| --- | --- |
| `scenario` | Matching scenario ID. |
| `noted` | Map of sections to required fact phrases. A current note must exist per section; each phrase must appear case-insensitively in a note of that section. |
| `open` | Sections requiring an open item and no current note. Cannot overlap `noted`. |
| `follow_ups` | Sections that must be asked about after the topic's first question. |
| `mentions` | Arrays of artifact IDs; each group must appear together in one question. |
| `max_questions` | Required maximum number of question turns. |
| `max_unsupported_terms` | Default 2. A note with more unsupported words/numbers is counted as invented. |
| `min_facts` | Required fraction of matched phrases, default 1.0. |

A scenario passes only if its session finished, all section/open/follow-up/mention
checks pass, the fact fraction meets the threshold, no note is invented and the
question count is within the golden maximum. Grounding uses
`eki.runbook.faithfulness.unsupported_terms` against non-confidential expert
turns of the topic, without treating question wording as support. Superseded
notes are excluded. These are deterministic lexical checks, not an LLM judge;
paraphrases may fail and false claims using the expert's words may pass.

### Reports and comparison

Each run prints a table per scenario and `N of M passed`, then saves a new
`reports/evals/<UTC time>.json`. `--report reports/evals/NAME.json` selects a new
path inside `reports/`; existing files are never overwritten. Reports record
time, git revision, model names, limits/timeouts, question caps, every check,
call/token usage, duration, redacted transcript, notes and open items. They do
not contain provider keys, DSNs, `.env` contents or raw exception messages.

`--compare PREVIOUS.json` prints previous/current pass states, changed numbers
and both totals. A previous pass that now fails or is absent counts as a
regression. Compare matching scenario selections when measuring model changes.

| Exit code | Meaning |
| --- | --- |
| 0 | All selected scenarios passed and no comparison regression, or dry-run validated successfully. |
| 1 | A scenario failed/aborted, or a previously passing scenario regressed. |
| 2 | Invalid inputs, output path or missing configuration/opt-in. |

Tests inject scripted models, including an end-to-end database driver check.
All eight drafts pass with the checklist double, including the existing planner's
G1 contradiction question. This is workflow evidence, not real-model quality
evidence. See [the runtime flow](../docs/flows/grade-interviews.md) and
[Phase 7 PDR](../docs/plans/phase-7.md).
