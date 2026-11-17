# Phase 10 proposal: configurable model providers

**Status:** proposed on 2026-11-16; awaiting approval before implementation.
**Proposed requirement F23:** independently configure hosted or local model
providers for each model role, with consistent runtime/evaluation configuration,
explicit capability requirements and isolated embedding spaces.

## Goal and verified baseline

Switch providers without changing interview, approval or retrieval logic. A
deployment may use different providers for drafting, interviewing, answer
selection, runbook embeddings and artifact embeddings.

The foundation already exists: `ChatModel` is a provider-independent protocol,
`LiteLLMModel` normalizes chat and tool calls, and `LiteLLMEmbedder` normalizes
embeddings. Five `EKI_MODEL_*` settings select models independently. LiteLLM is
already pinned at 1.108.1; this phase needs no additional dependency or gateway
service. The current example configuration emphasizes Groq.

Missing pieces verified in the repository:

- Chat and embedding constructors do not accept explicit per-role endpoint,
  credential or API-version configuration. Model construction is duplicated in
  runtime services, live tests and the grader.
- Model limits use a shared timeout and a fixed chat output cap. There is no
  operator-facing compatibility check for a selected deployment.
- Artifact queries reconstruct a model from its saved identity. Embedding
  identities currently contain only the model string and 256 dimensions;
  identical model names served at different endpoints could otherwise collide.
- Plan-generation recovery fingerprints must reflect settings that change
  model behavior, including a custom endpoint and output cap.
- Live evaluation consent currently depends on a fixed credential-name list;
  keyless local endpoints and custom credential names need explicit handling.

## Proposed implementation

### 10a: one configuration and construction path

Keep all existing `EKI_MODEL_*` settings compatible. Add an optional typed
`EKI_MODEL_OPTIONS` JSON mapping keyed by `interviewer`, `drafter`, `answerer`,
`embedding` and `artifact_embedding`. Supported fields are `api_base`,
`api_key_env`, `api_version`, `timeout_seconds` and, for chat, `max_tokens`.
Absent options retain current behavior. Reject unknown roles/options, invalid
URLs and nonpositive limits; credentials remain environment references rather
than inline JSON secrets. Provider-native credentials continue to work.

Build chat and embedding adapters through shared factories used by production,
the grader and live tests. Resolve explicit credentials at the provider boundary,
keep values masked, and fail clearly when a configured credential is missing.
Pass only supported, allowlisted options to LiteLLM. Do not forward arbitrary
SDK kwargs or silently drop tools. Retain redaction and sanitized exceptions.

### 10b: compatibility, identity and recovery

Add a local `eki models check` command that prints the selected roles, model
names, limits, credential availability and capability status without making
provider calls or displaying credentials. Use the pinned LiteLLM metadata where
available. Distinguish unknown custom deployments from known incompatible ones;
metadata is a preflight hint, not evidence that a deployment works.

The interviewer requires tool calling. Drafting and answer selection retain
their existing validated output contracts. Embedding providers must produce
finite 256-dimensional vectors; do not truncate arbitrary vectors to fit.

Preserve legacy embedding identities for configurations without endpoint
overrides. New custom deployments get a stable identity incorporating the model,
endpoint and API version, with no credential values. Changing credentials alone
must not invalidate an index. A saved custom artifact identity must resolve to
its matching configuration or fail with an explicit reindex/configuration error;
never send queries into a different vector space. Runbook indexes remain
separated by identity. Include behavior-changing chat configuration in plan
generation fingerprints so unfinished generation cannot silently mix settings.

Keep provider selection explicit. Automatic provider fallback, load balancing
and provider changes during a model attempt are outside this phase. Verify SDK
retry behavior against the existing attempt budget and audit contract.

### 10c: provider recipes and acceptance evidence

Document configuration recipes for OpenAI, Anthropic, Gemini, Groq, Azure OpenAI
and Ollama/OpenAI-compatible endpoints, including a mixed-provider example.
Use operator-selected model/deployment placeholders where capability depends
on the model; listing a provider does not certify all its models.

Update live-test/grader configuration to use the same factories and make
keyless/custom-provider opt-in explicit. Preserve the rule that `.env` contents
alone do not authorize live tests. Existing call budgets continue to apply.
No live provider calls are included in approval of this implementation phase;
those require separately selected models and an agreed call/token budget.

## Acceptance criteria

1. Existing Groq configuration works unchanged; optional offline answer and
   embedding defaults remain available.
2. Synthetic contract tests exercise different providers across roles and
   custom endpoints through the shared factories, including runtime and eval
   construction paths. No real credentials or network calls are needed.
3. Tool-call/result round trips, usage normalization, malformed replies,
   provider failures and redaction retain their existing guarantees.
4. Invalid configuration fails before a provider call. Diagnostics distinguish
   known unsupported capabilities from unknown deployment capabilities.
5. Endpoint changes cannot reuse incompatible vectors or resume draft
   generation under the old configuration fingerprint. Credential rotation
   preserves compatible model/index identities.
6. Secrets and provider payloads do not appear in diagnostics, audit, traces,
   fingerprints or sanitized errors. Tests cover custom credential handling.
7. Targeted tests, deterministic quality gate, ruff and mypy pass; run the
   affected database recovery/retrieval tests where local infrastructure is
   available. Record actual results and any remaining live-validation gap.

## Boundaries and references

This phase changes provider configuration and adapter integration. The workflow,
prompts, access control, publication approval and citation enforcement retain
their current responsibilities. Human-owned evaluation files are untouched.
Provider portability does not establish equal answer quality across models;
the existing scorecard is the later acceptance mechanism for each deployment.

Primary references checked while preparing this proposal:

- [LiteLLM supported providers](https://docs.litellm.ai/docs/providers)
- [OpenAI-compatible endpoints](https://docs.litellm.ai/docs/providers/openai_compatible)
- [Tool-calling capability checks](https://docs.litellm.ai/docs/completion/function_call)
- Local pinned adapter source under `.venv/lib/python3.12/site-packages/litellm/`.

Implementation approval is required by `CLAUDE.md`'s working agreement:
"Plan one phase at a time, save it to `docs/plans/phase-N.md`, and wait for
human approval before writing implementation code."
