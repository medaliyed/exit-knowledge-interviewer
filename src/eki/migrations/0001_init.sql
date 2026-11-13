-- Phase 1 schema: one ingest run is one immutable snapshot of what was read, mapped and scored.
-- Everything stored here has already been redacted.

-- In the public schema, so every schema on the search path can use the type.
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

CREATE TABLE ingest_runs (
    id            uuid PRIMARY KEY,
    expert_name   text        NOT NULL,
    expert_email  text        NOT NULL,
    since         timestamptz NOT NULL,
    until         timestamptz NOT NULL,
    status        text        NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);

CREATE TABLE artifacts (
    run_id           uuid        NOT NULL REFERENCES ingest_runs (id) ON DELETE CASCADE,
    id               text        NOT NULL,
    source           text        NOT NULL,
    kind             text        NOT NULL,
    system           text,
    occurred_at      timestamptz NOT NULL,
    title            text        NOT NULL,
    body             text        NOT NULL,
    expert_role      text        NOT NULL,
    metadata         jsonb       NOT NULL,
    redaction_counts jsonb       NOT NULL,
    PRIMARY KEY (run_id, id)
);

-- Counts of other people's work, with no content. Used to measure how exclusive the expert was.
CREATE TABLE activity (
    run_id       uuid        NOT NULL REFERENCES ingest_runs (id) ON DELETE CASCADE,
    scope        text        NOT NULL CHECK (scope IN ('repo', 'path', 'ticket_group')),
    system       text        NOT NULL,
    key          text        NOT NULL,
    total        integer     NOT NULL,
    by_expert    integer     NOT NULL,
    other_actors integer     NOT NULL,
    last_at      timestamptz,
    PRIMARY KEY (run_id, scope, system, key)
);

CREATE TABLE chunks (
    run_id      uuid         NOT NULL,
    artifact_id text         NOT NULL,
    ordinal     integer      NOT NULL,
    text        text         NOT NULL,
    embedding   vector(256)  NOT NULL,
    PRIMARY KEY (run_id, artifact_id, ordinal),
    FOREIGN KEY (run_id, artifact_id) REFERENCES artifacts (run_id, id) ON DELETE CASCADE
);
CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE topics (
    run_id uuid NOT NULL REFERENCES ingest_runs (id) ON DELETE CASCADE,
    id     text NOT NULL,
    kind   text NOT NULL,
    system text,
    label  text NOT NULL,
    PRIMARY KEY (run_id, id)
);

CREATE TABLE topic_artifacts (
    run_id      uuid NOT NULL,
    topic_id    text NOT NULL,
    artifact_id text NOT NULL,
    PRIMARY KEY (run_id, topic_id, artifact_id),
    FOREIGN KEY (run_id, topic_id) REFERENCES topics (run_id, id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, artifact_id) REFERENCES artifacts (run_id, id) ON DELETE CASCADE
);

CREATE TABLE gap_scores (
    run_id           uuid             NOT NULL,
    topic_id         text             NOT NULL,
    exclusivity      double precision NOT NULL,
    doc_gap          double precision NOT NULL,
    recency          double precision NOT NULL,
    criticality      double precision NOT NULL,
    score            double precision NOT NULL,
    doc_status       text             NOT NULL CHECK (doc_status IN ('none', 'partial', 'documented')),
    linked_documents jsonb            NOT NULL,
    explanation      jsonb            NOT NULL,
    PRIMARY KEY (run_id, topic_id),
    FOREIGN KEY (run_id, topic_id) REFERENCES topics (run_id, id) ON DELETE CASCADE
);
