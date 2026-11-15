-- F22: explicitly saved reader questions and triage state for public open sections.
-- Open-section candidates are derived live from current entries; a row exists for one
-- only once it has been triaged or scheduled, under the stable id uuid5(entry_id, section).
CREATE TABLE follow_up_gaps (
 id uuid PRIMARY KEY,
 run_id uuid NOT NULL REFERENCES ingest_runs(id) ON DELETE CASCADE,
 kind text NOT NULL CHECK (kind IN ('open_section','request')),
 entry_id uuid REFERENCES runbook_entries(id) ON DELETE CASCADE,
 section text CHECK (section IN ('trigger','diagnosis','steps','rollback','contacts','pitfalls','rationale')),
 requester_subject text, request_id uuid, question text,
 created_at timestamptz NOT NULL,
 status text NOT NULL DEFAULT 'pending' CHECK (status IN
  ('pending','scheduled','awaiting_review','resolved','deferred','dismissed')),
 target_entry_id uuid REFERENCES runbook_entries(id) ON DELETE SET NULL,
 target_section text CHECK (target_section IN ('trigger','diagnosis','steps','rollback','contacts','pitfalls','rationale')),
 new_topic boolean NOT NULL DEFAULT false,
 topic_label text, seed_question text,
 priority integer NOT NULL DEFAULT 0,
 reason text,
 group_id uuid,
 revision integer NOT NULL DEFAULT 0,
 resolved_entry_id uuid, resolved_version integer, resolved_at timestamptz, resolved_by_subject text,
 CHECK ((kind='request') = (requester_subject IS NOT NULL AND request_id IS NOT NULL AND question IS NOT NULL)),
 CHECK (kind<>'open_section' OR (entry_id IS NOT NULL AND section IS NOT NULL)),
 UNIQUE (run_id, requester_subject, request_id)
);
CREATE INDEX follow_up_gaps_run ON follow_up_gaps(run_id, status);
CREATE TABLE follow_up_throttle (subject text PRIMARY KEY, window_at timestamptz NOT NULL, count integer NOT NULL);
