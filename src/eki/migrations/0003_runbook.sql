-- Phase 3a: private runbook drafts and expert review history. Nothing is published.
ALTER TABLE turns DROP CONSTRAINT turns_kind_check;
ALTER TABLE turns ADD CONSTRAINT turns_kind_check
    CHECK (kind IN ('question', 'answer', 'skip', 'correction', 'edit'));

CREATE TABLE runbook_entries (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    topic_id text NOT NULL,
    title text NOT NULL,
    status text NOT NULL CHECK (status IN ('draft', 'approved', 'rejected')),
    content jsonb NOT NULL,
    approved_by text,
    approved_at timestamptz,
    rejection_reason text,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    UNIQUE (session_id, topic_id),
    CHECK ((status = 'approved') = (approved_by IS NOT NULL AND approved_at IS NOT NULL)),
    CHECK (status <> 'rejected' OR rejection_reason IS NOT NULL)
);

CREATE TABLE entry_decisions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_id uuid NOT NULL REFERENCES runbook_entries (id) ON DELETE CASCADE,
    action text NOT NULL CHECK (action IN ('approve', 'reject', 'edit', 'correction')),
    who text NOT NULL,
    at timestamptz NOT NULL,
    reason text
);
