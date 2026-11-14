-- Durable metadata only. No FK to runs: deleting application data retains history.
CREATE TABLE audit_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id uuid NOT NULL UNIQUE,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    correlation_id uuid NOT NULL,
    call_id uuid,
    run_id uuid,
    user_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('model','tool','approval','review','access','ingest')),
    phase text NOT NULL CHECK (phase IN ('started','completed','failed','refused','skipped')),
    details jsonb NOT NULL
);
CREATE INDEX audit_run_idx ON audit_events (run_id, id);
CREATE INDEX audit_call_idx ON audit_events (call_id);
CREATE FUNCTION refuse_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit events are append-only' USING ERRCODE = '55000';
END;
$$;
CREATE TRIGGER audit_no_changes BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_events
FOR EACH STATEMENT EXECUTE FUNCTION refuse_audit_mutation();
