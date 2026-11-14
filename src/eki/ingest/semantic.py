"""Artifact embedding configuration; each query uses the run's recorded model."""

from uuid import UUID

import psycopg

from eki.audit import AuditRecorder
from eki.config import Settings, work_domains
from eki.ingest.embedding import HashingEmbedder
from eki.ingest.index import recorded_model
from eki.llm import ModelError
from eki.search.embedding import AuditedEmbedder, LiteLLMEmbedder, SearchEmbedder


def configured(
    settings: Settings, *, run_id: UUID | None = None, identity: str | None = None
) -> SearchEmbedder:
    model: str | None
    if identity == HashingEmbedder.name:
        return HashingEmbedder()
    if identity is not None:
        if not identity.startswith("litellm:") or not identity.endswith(":256"):
            raise ModelError("artifact index uses an unsupported model identity")
        model = identity[len("litellm:") : -len(":256")]
    else:
        model = settings.model_artifact_embedding
    if not model:
        return HashingEmbedder()
    return AuditedEmbedder(
        LiteLLMEmbedder(model, settings.model_timeout_seconds, work_domains(settings)),
        AuditRecorder(settings.database_url.get_secret_value()),
        run_ids=None if run_id is None else (run_id,),
    )


def for_run(conn: psycopg.Connection, run_id: UUID, settings: Settings) -> SearchEmbedder:
    identity = recorded_model(conn, run_id)
    if identity is None:
        raise ModelError("artifact run not found")
    return configured(settings, run_id=run_id, identity=identity)
