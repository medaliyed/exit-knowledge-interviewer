# Phase 5 live validation record

Status: pending by the user's explicit instruction on 2026-11-14 to leave live
validation pending. F15 and the live-quality portion of F17 are not complete.

## Availability check

The existing guarded tests were selected on 2026-11-14 while F14 was being
implemented (base revision `a630b6e`; F14 subsequently committed as `e0f4e7c`):

```sh
.venv/bin/pytest -q -m live -rs tests/test_live_model.py tests/test_live_retrieval.py \
  --junitxml=reports/phase5-live-initial.xml
```

| Check | Result | Reason |
| --- | --- | --- |
| Interview follow-up, unknown knowledge and contradiction | Skipped | No provider credential exported to the test process |
| Semantic runbook retrieval | Skipped | `EKI_MODEL_EMBEDDING` not configured |
| Grounded evidence selector and abstention | Skipped | `EKI_MODEL_ANSWERER` not configured |

Result: **3 skipped in 0.38 seconds**, no provider calls, no provider token usage.
The generated JUnit report is in the ignored `reports/` directory. Sources were
synthetic and database fixtures used disposable schemas. The metadata check
reported drafter/interviewer roles configured, no embedding/answerer role, and
no exported provider credential; no secret values were read into this record.

## Resume criteria

Before running paid tests, the operator must select the model identifiers,
export credentials locally and provide a call/token budget. Then execute each
applicable check in three recorded runs, preserving failures and reruns, model
configuration, revision, usage and durations. Add the artifact-retrieval checks
from F17 to the same evidence record. A skip or a scripted-provider result does
not count as real-model acceptance evidence.

Default tests and CI continue to use scripted providers. Human-owned
`evals/scenarios/` and `evals/golden/` remain untouched.


The F17 implementation added `tests/test_live_artifacts.py`: three repetitions
of three fixed paraphrase cases, with distractor artifacts and separate offline
hashing/semantic results. This suite has not been run against a provider; it is
covered by the user's instruction to keep live validation pending.
