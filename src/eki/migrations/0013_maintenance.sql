-- F21: explicit responsibility, attributed edits, review lifecycle and reader feedback.
ALTER TABLE ingest_runs ADD COLUMN maintenance_started_at timestamptz;
ALTER TABLE ingest_runs ADD COLUMN maintenance_revision integer NOT NULL DEFAULT 0;
CREATE TABLE handovers (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 run_id uuid NOT NULL,
 previous_owner text, owner_subject text NOT NULL, actor_subject text NOT NULL,
 at timestamptz NOT NULL, reason text NOT NULL, started_maintenance boolean NOT NULL
);
CREATE TRIGGER handovers_no_changes BEFORE UPDATE OR DELETE ON handovers
 FOR EACH STATEMENT EXECUTE FUNCTION refuse_audit_mutation();
ALTER TABLE turns DROP CONSTRAINT turns_speaker_check;
ALTER TABLE turns ADD CONSTRAINT turns_speaker_check CHECK (speaker IN ('interviewer','expert','maintainer'));
ALTER TABLE turns ADD COLUMN author_subject text;
ALTER TABLE turns ADD COLUMN author_name text;
ALTER TABLE turns ADD CONSTRAINT maintainer_attribution CHECK
 (speaker <> 'maintainer' OR (kind='edit' AND NOT confidential AND author_subject IS NOT NULL AND author_name IS NOT NULL));
ALTER TABLE entry_decisions DROP CONSTRAINT entry_decisions_action_check;
ALTER TABLE entry_decisions ADD CONSTRAINT entry_decisions_action_check CHECK
 (action IN ('approve','reject','edit','correction','review','withdraw'));
ALTER TABLE entry_decisions ADD COLUMN actor_subject text;
ALTER TABLE entry_decisions ADD COLUMN version integer;
ALTER TABLE runbook_entries ADD COLUMN maintenance_revision integer NOT NULL DEFAULT 0;
ALTER TABLE runbook_entries ADD COLUMN review_by date;
ALTER TABLE runbook_entries ADD COLUMN last_reviewed_at timestamptz;
ALTER TABLE runbook_entries ADD COLUMN last_reviewed_by_subject text;
ALTER TABLE runbook_entries ADD COLUMN last_reviewed_by_name text;
ALTER TABLE runbook_entries ADD COLUMN reviewed_version integer;
UPDATE runbook_entries SET review_by=(approved_at AT TIME ZONE 'UTC')::date + 90,
 last_reviewed_at=approved_at,last_reviewed_by_name=approved_by,reviewed_version=version
 WHERE status='approved';
CREATE TABLE entry_flags (
 id uuid PRIMARY KEY, entry_id uuid NOT NULL REFERENCES runbook_entries(id) ON DELETE CASCADE,
 reporter_subject text NOT NULL, request_id uuid NOT NULL, observed_version integer NOT NULL,
 category text NOT NULL CHECK (category IN ('incorrect','outdated','unclear')),
 section text CHECK (section IN ('trigger','diagnosis','steps','rollback','contacts','pitfalls','rationale')),
 description text NOT NULL, created_at timestamptz NOT NULL,
 resolution text CHECK (resolution IN ('corrected','confirmed_current','dismissed')),
 resolved_at timestamptz, resolved_by text, resolution_note text, resolved_version integer,
 UNIQUE(entry_id,reporter_subject,request_id)
);
CREATE INDEX entry_flags_open ON entry_flags(entry_id,created_at) WHERE resolution IS NULL;
CREATE TABLE flag_throttle (subject text PRIMARY KEY, window_at timestamptz NOT NULL, count integer NOT NULL);
