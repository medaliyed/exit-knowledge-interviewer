CREATE TABLE plan_generations (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES ingest_runs(id) ON DELETE CASCADE,
    fingerprint text NOT NULL,
    selected_plan jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('running','needs_retry','completed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX plan_generations_run_idx ON plan_generations(run_id, created_at);
CREATE TABLE generation_topics (
    generation_id uuid NOT NULL REFERENCES plan_generations(id) ON DELETE CASCADE,
    topic_id text NOT NULL,
    result jsonb NOT NULL,
    PRIMARY KEY (generation_id, topic_id)
);
ALTER TABLE audit_events DROP CONSTRAINT audit_events_kind_check;
ALTER TABLE audit_events ADD CONSTRAINT audit_events_kind_check
    CHECK (kind IN ('model','tool','approval','review','access','ingest','delivery','generation'));
