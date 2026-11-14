-- Existing runs are deliberately unassigned: only explicit admins can see them.
ALTER TABLE ingest_runs ADD COLUMN owner_subject text;
CREATE TABLE run_readers (
    run_id uuid NOT NULL REFERENCES ingest_runs (id) ON DELETE CASCADE,
    subject text NOT NULL CHECK (length(btrim(subject)) > 0),
    PRIMARY KEY (run_id, subject)
);
CREATE INDEX run_readers_subject_idx ON run_readers (subject, run_id);
