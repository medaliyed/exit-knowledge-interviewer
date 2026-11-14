-- Only approved entries are indexed; every query also checks live approval, ACL and version.
CREATE TABLE runbook_index (
    entry_id uuid NOT NULL REFERENCES runbook_entries (id) ON DELETE CASCADE,
    section text NOT NULL CHECK (section IN
        ('trigger','diagnosis','steps','rollback','contacts','pitfalls','rationale')),
    ordinal integer NOT NULL,
    version integer NOT NULL,
    model text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    embedding vector(256) NOT NULL,
    document tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english'::regconfig, title), 'A') ||
        setweight(to_tsvector('english'::regconfig, body), 'B')
    ) STORED,
    PRIMARY KEY (entry_id, section, ordinal, model)
);
CREATE INDEX runbook_document_idx ON runbook_index USING gin (document);

CREATE FUNCTION invalidate_runbook_index() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM runbook_index WHERE entry_id = OLD.id;
    RETURN NEW;
END;
$$;
CREATE TRIGGER runbook_index_invalidation AFTER UPDATE ON runbook_entries
FOR EACH ROW EXECUTE FUNCTION invalidate_runbook_index();
