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

Real-provider checks are separate, explicitly opt-in, and may incur charges:
`uv run pytest -m live -s -rs tests/test_live_retrieval.py`. Configure the model
roles and export credentials to the test process; missing configuration skips
with a reason. Skips are not evidence of real-provider quality.
