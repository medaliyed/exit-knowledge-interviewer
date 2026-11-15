# Phase 7 PDR: golden interview evals

**Status:** proposed 2026-11-15; implementation started the same day on the
owner's request ("create a PDR for this feature, update the docs, then
implement"). Verification evidence goes in the Outcome section when the phase
is done.

**Parent:** [Project PDR](../PDR.md), new requirement **F20**. It builds on the
opt-in live checks of F15 ([phase-5-live.md](phase-5-live.md)) and on the
synthetic quality gate of Phase 4 (`evals/run.py`). All existing redaction,
confidentiality, approval, access and audit rules still apply: the grader
drives the real `InterviewService`, so nothing is bypassed.

**Goal:** a repeatable scorecard for the interviewer model. Each planted gap in
the synthetic corpus becomes one graded interview; a run prints and saves a
report card; two reports can be compared, so a model, prompt or tool change is
measured instead of guessed.

## 1. Problem and baseline

Every automated test drives the interviewer with a scripted double
(`tests/fakes.py`: `ScriptedModel`, `checklist_interviewer`). Those tests prove
the plumbing: answers are stored, sessions resume, confidential text never
reaches a model. They say nothing about the judgement of a real model.

| Gap in the current implementation | Consequence |
| --- | --- |
| The only real-model interview check (`tests/test_live_model.py`) has never been run and covers three behaviours on three gaps. | Interview quality is unknown. |
| A live test is pass/fail with no record. | Two runs cannot be compared; a regression after a prompt or model change is invisible while every scripted test stays green. |
| `evals/scenarios/` and `evals/golden/` are named in `CLAUDE.md` and in CI docs but do not exist, and nothing reads them. | The human-owned evaluation content has no format and no consumer. |
| Cost is unbounded: a looping interviewer can burn a provider budget. | Nobody wants to start a paid run. |

**Baseline that stays the same:** the interviewer node, its six tools, the
seven-section template, `InterviewService` and the synthetic corpus in
`tests/fixtures/corpus` (eight planted gaps G1–G8, traps T1–T7, the scripted
expert answers in `tests/fixtures/expert_answers.json`). The pipeline still
never reads `answer_key.json` or `expert_answers.json`.

## 2. Users and outcomes

| User | Outcome |
| --- | --- |
| **Maintainer** | Runs `uv run python evals/grade.py`, reads one table per scenario and a final pass count, and keeps the JSON report. |
| **Maintainer changing a prompt or model** | Runs the grader before and after and compares the two reports. |
| **Owner of the evals** (human) | Writes the scenario and golden files in `evals/scenarios/` and `evals/golden/`. Drafts are provided for review; nothing is written there by code or by an agent. |
| **CI** | Unchanged. The grader needs a provider key and is never run in CI. |

## 3. Scope

**In scope**

- A scenario format: which corpus and expert, which planted topic (by artifact
  IDs), and the simulated expert's script for that topic.
- A golden format: the facts per section a good interview must capture, the
  sections that must be left open, the follow-ups and contradictions it must
  ask about, a question budget and a grounding threshold.
- A grader that runs one real interview per scenario through
  `InterviewService` with the configured models, scores the resulting session
  against its golden, prints a scorecard and saves a JSON report.
- A compare mode for two reports.
- A per-scenario model-call budget that stops a runaway interview.
- The same explicit opt-in as the live tests: model roles configured and a
  provider key exported to the process. A `.env` entry alone is not consent.
- A `--dry-run` that validates files and lists what would run, with no database
  and no model.
- Drafts of eight scenarios and eight goldens, one per planted gap, placed
  under `docs/plans/phase-7-drafts/` for the owner to review, edit and move.
- Tests for the grading logic with scripted doubles, including one end-to-end
  database test of the driver with the scripted checklist interviewer.

**Out of scope for this phase**

- An LLM-as-judge. Scoring is deterministic string and structure matching.
- Grading plan drafting, synthesis, search or Q&A. Those have their own checks.
- Running the grader in CI or on a schedule.
- A browser view of reports.
- Statistical confidence across repetitions. Each run is one sample; the
  report records enough to repeat it.

## 4. Requirements

### 4.1 Scenario and golden files

| ID | Requirement |
| --- | --- |
| E1 | A scenario is one JSON file in `evals/scenarios/`, validated by a strict model: `id`, `title`, `corpus` (path relative to the repository root), `expert` (name, email, work domains), `span` (since, until), `topic_artifact_ids`, `script` (`initial` per section, `follow_ups` per section, `freeform`), `question_cap` (hard stop, default 30). |
| E2 | A golden is one JSON file in `evals/golden/`, named after its scenario: `scenario`, `noted` (section → phrases that must appear in a note of that section), `open` (sections that must be left open and must not get a note), `follow_ups` (sections that must be asked about after the first question), `mentions` (groups of artifact IDs that must appear together in one question), `max_questions`, `max_unsupported_terms` (default 2), `min_facts` (default 1.0). |
| E3 | Sections are the seven template sections and nothing else. Unknown keys or sections are rejected at load time, with the file name in the error. |
| E4 | The grader reads these directories and never creates, edits or deletes anything in them. Reports go to `reports/`, which is ignored by git. |
| E5 | A scenario without a golden, or a golden without a scenario, is a load error. |

### 4.2 Running a scenario

| ID | Requirement |
| --- | --- |
| R1 | Each scenario runs in its own throwaway Postgres schema, migrated and dropped afterwards, so scenarios cannot affect each other or real data. |
| R2 | The corpus is ingested through the real read-only MCP servers, mapped and scored with the real code; the scenario's topic is the one holding at least 80% of `topic_artifact_ids`. A scenario whose topic is not found fails with that reason. |
| R3 | Only that topic is saved to the run, so the plan contains exactly one topic. The plan is drafted and approved with the real `InterviewService`; the simulated expert answers every question from the script until the session finishes or `question_cap` questions were asked. |
| R4 | Both models are wrapped in a budget: after `--max-calls` model calls (default 40) the next call fails with a `ModelError`, the interview stops and the scenario is reported as aborted with its budget reason. |
| R5 | A scenario that raises for any other reason is reported as failed with the exception type; the remaining scenarios still run. |
| R6 | The grader refuses to start, before any database or provider work, when a model role is missing or no provider key is exported. The message says which. |

### 4.3 Scoring

| ID | Requirement |
| --- | --- |
| S1 | `sections`: for every section in `noted`, a non-superseded note exists. Reported as found/expected. |
| S2 | `facts`: for every phrase in `noted[section]`, a note of that section contains it, case-insensitively. Reported as found/expected. |
| S3 | `open`: every section in `open` has an open item and no note. A note in such a section counts as filled in. |
| S4 | `follow_ups`: for every section in `follow_ups`, a question of that section exists that is not the topic's first question. |
| S5 | `mentions`: for every group in `mentions`, one question text contains every ID of the group. |
| S6 | `invented`: the number of notes whose text has more than `max_unsupported_terms` terms (words or numbers) found in none of the expert's non-confidential turns of that topic, using `eki.runbook.faithfulness.unsupported_terms`. |
| S7 | `questions`: the number of question turns, compared with `max_questions`. |
| S8 | A scenario passes when the session finished, every `sections`, `open`, `follow_ups` and `mentions` check holds, `facts` ≥ `min_facts`, `invented` is 0 and `questions` ≤ `max_questions`. |
| S9 | Scoring is a pure function of the session view and the golden; it is unit-tested with hand-built views. |

### 4.4 Reports

| ID | Requirement |
| --- | --- |
| P1 | The scorecard is printed as one table per scenario (check, result, pass) and a final line `N of M passed`. |
| P2 | The JSON report records: time, git revision, model names, settings that affect the interview (limits, timeout), per scenario every check with its numbers, question count, model calls, input and output tokens, duration, the transcript (questions and the simulated answers) and the notes and open items. |
| P3 | The report contains redacted text only, as the session view does. The scripted answers are synthetic. No credentials and no `.env` values are written. |
| P4 | `--compare PREVIOUS.json` prints, per scenario, the previous and current pass state and the numbers that changed, and both totals. The exit code is 1 when a scenario that passed before fails now. |
| P5 | The exit code is 0 when every scenario passed, 1 otherwise, 2 for a usage or guard error. |

## 5. CLI

```bash
uv run python evals/grade.py                          # every scenario, real models
uv run python evals/grade.py --only G3-month-end      # one scenario
uv run python evals/grade.py --dry-run                # validate and list, no DB, no model
uv run python evals/grade.py --max-calls 25           # tighter budget per scenario
uv run python evals/grade.py --compare reports/evals/2026-11-15T10-00-00Z.json
uv run python evals/grade.py --scenarios docs/plans/phase-7-drafts/scenarios \
                              --golden docs/plans/phase-7-drafts/golden   # grade the drafts
```

Defaults: `--scenarios evals/scenarios`, `--golden evals/golden`,
`--report reports/evals/<UTC time>.json`.

Required environment: `EKI_DATABASE_URL`, `EKI_MODEL_INTERVIEWER`,
`EKI_MODEL_DRAFTER`, and the provider key exported to the shell.

## 6. Code layout

```
src/eki/evals/
  __init__.py
  models.py     Scenario, Golden, ScenarioResult, Report (pydantic, frozen, extra=forbid)
  expert.py     SimulatedExpert (moved from tests/fakes.py, which re-exports it)
  budget.py     BudgetedModel: a ChatModel wrapper that fails after N calls
  guard.py      PROVIDER_KEYS, has_provider_key (moved from tests/live_support.py)
  grading.py    grade(view, topic_id, golden) -> ScenarioResult
  report.py     render(report), compare(previous, current), save(report, path)
  driver.py     load_pairs(dirs), run_scenario(...), run_all(...)
evals/grade.py  argument parsing, guard, settings, exit codes
docs/plans/phase-7-drafts/scenarios/*.json, golden/*.json   drafts for the owner
```

`eki.evals` never imports `tests`. The driver takes the models as parameters so
tests can inject scripted doubles; `evals/grade.py` builds the LiteLLM models
from settings.

## 7. Security and privacy

- The grader is a client of the real service: redaction, audit and
  confidentiality behave as in production. Audit events of a scenario live in
  its throwaway schema and are dropped with it.
- Scenario scripts are synthetic. The drafts are derived from
  `tests/fixtures/expert_answers.json`, which is already synthetic.
- Paid calls need the same explicit consent as the live tests. The guard runs
  before any work.
- Nothing is written outside `reports/` and the throwaway schema.

## 8. Acceptance criteria

| # | Criterion |
| --- | --- |
| A1 | Loading a scenario with an unknown section or key fails and names the file. |
| A2 | `grade()` on a hand-built finished view with every expectation met passes; removing the rollback follow-up, filling in an `open` section, adding a note with three unsupported terms, or exceeding `max_questions` each fails exactly the matching check. |
| A3 | `BudgetedModel` passes through N calls and raises `ModelError` on call N+1 without calling the wrapped model. |
| A4 | The driver, with the scripted checklist interviewer and quoting drafter, runs a draft scenario end to end against Postgres and produces a report whose scenario finished with seven notes (db test). |
| A5 | `--dry-run` on the drafts lists eight scenarios and performs no database or model work. |
| A6 | With no provider key exported, `evals/grade.py` exits 2 before connecting to anything; a key in `.env` alone does not change that. |
| A7 | `compare()` flags a scenario that passed before and fails now, and the script returns 1 for it. |
| A8 | `evals/scenarios/` and `evals/golden/` are untouched by the implementation: `git status` shows no file there. |
| A9 | pytest, ruff and mypy pass; the existing tests that import `SimulatedExpert` and `has_provider_key` from `tests` still pass. |

## 9. Deliverables and order

1. **7a** Models, expert, budget, guard and grading, with unit tests (E1–E3, S1–S9, A1–A3).
2. **7b** Driver, report, compare and the `evals/grade.py` script, with the db test and guard tests (R1–R6, P1–P5, A4–A7).
3. **7c** Draft scenarios and goldens for G1–G8 under `docs/plans/phase-7-drafts/`; docs (PDR, CLAUDE.md, README, evals/README.md, flows index).

One commit per deliverable, tagged F20.

## 10. Open questions for the owner

- Whether to adopt the drafts as they are. Until they are moved into
  `evals/scenarios/` and `evals/golden/`, the grader has nothing to run by
  default and must be pointed at the drafts.
- The first paid run: model names and a call budget. The default of 40 calls
  per scenario and eight scenarios is roughly 320 calls, in the order of
  magnitude of the single existing live test times ten.
