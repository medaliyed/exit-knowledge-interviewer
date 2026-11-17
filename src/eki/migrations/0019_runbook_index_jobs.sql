-- Only model identities are registered: no credentials, endpoints or source text.
CREATE TABLE runbook_index_models (model text PRIMARY KEY);
INSERT INTO runbook_index_models VALUES ('hashing-blake2b-v1-256');

CREATE TABLE runbook_index_jobs (
    entry_id uuid NOT NULL REFERENCES runbook_entries(id) ON DELETE CASCADE,
    version integer NOT NULL,
    model text NOT NULL REFERENCES runbook_index_models(model) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','processing','retrying','ready','obsolete')),
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    error_code text CHECK (error_code IN ('provider_unavailable','audit_unavailable')),
    PRIMARY KEY (entry_id, version, model)
);
CREATE INDEX runbook_index_jobs_due ON runbook_index_jobs(model, available_at)
    WHERE status IN ('pending','processing','retrying');

CREATE FUNCTION enqueue_runbook_index() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE runbook_index_jobs SET status='obsolete',updated_at=now()
        WHERE entry_id=NEW.id AND (version<>NEW.version OR NEW.status<>'approved')
        AND status<>'obsolete';
    IF NEW.status='approved' THEN
        INSERT INTO runbook_index_jobs(entry_id,version,model)
            SELECT NEW.id,NEW.version,model FROM runbook_index_models
            ON CONFLICT (entry_id,version,model) DO UPDATE SET status='pending',
                available_at=now(),updated_at=now(),error_code=NULL;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER runbook_index_enqueue_insert AFTER INSERT ON runbook_entries
    FOR EACH ROW EXECUTE FUNCTION enqueue_runbook_index();
CREATE TRIGGER runbook_index_enqueue_update AFTER UPDATE ON runbook_entries
    FOR EACH ROW WHEN (
        OLD.version IS DISTINCT FROM NEW.version OR OLD.status IS DISTINCT FROM NEW.status
        OR OLD.content IS DISTINCT FROM NEW.content OR OLD.title IS DISTINCT FROM NEW.title
    ) EXECUTE FUNCTION enqueue_runbook_index();

-- Backfill without provider work. Existing valid indexes remain immediately usable.
INSERT INTO runbook_index_jobs(entry_id,version,model,status)
    SELECT e.id,e.version,m.model,
        CASE WHEN EXISTS (SELECT 1 FROM runbook_index i WHERE i.entry_id=e.id
            AND i.version=e.version AND i.model=m.model) THEN 'ready' ELSE 'pending' END
    FROM runbook_entries e CROSS JOIN runbook_index_models m WHERE e.status='approved';
