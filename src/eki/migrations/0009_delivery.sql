-- Durable delivery refers to protected, already-redacted transcript turns.
CREATE TABLE message_receipts (
    session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    id uuid NOT NULL,
    turn_ordinal integer NOT NULL,
    status text NOT NULL CHECK (status IN ('saved','processing','needs_retry','processed')),
    initial_event jsonb,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, id),
    UNIQUE (session_id, turn_ordinal),
    FOREIGN KEY (session_id, turn_ordinal) REFERENCES turns(session_id, ordinal) ON DELETE CASCADE,
    CHECK ((status = 'processed') = (result IS NOT NULL))
);
ALTER TABLE audit_events DROP CONSTRAINT audit_events_kind_check;
ALTER TABLE audit_events ADD CONSTRAINT audit_events_kind_check
    CHECK (kind IN ('model','tool','approval','review','access','ingest','delivery'));
-- Replay the same redacted model reply after a crash in its tool sequence.
CREATE TABLE interview_replies (
    session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    step integer NOT NULL,
    call_index integer NOT NULL,
    reply jsonb NOT NULL,
    PRIMARY KEY (session_id, step, call_index)
);
