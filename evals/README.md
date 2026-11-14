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
