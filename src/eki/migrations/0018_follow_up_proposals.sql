-- F22 9c: private follow-up proposals for existing entries. A proposal row is also the
-- explicit entry <- follow-up session/topic lineage that citation validation accepts.
-- Additive: existing plans, sessions, entries, citations and approvals are untouched (C7);
-- no proposal is fabricated for sessions finished before this migration.
CREATE TABLE follow_up_proposals (
 id uuid PRIMARY KEY,                       -- uuid5(session_id, 'proposal:' || topic_id)
 session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,   -- follow-up session
 topic_id text NOT NULL,                    -- = target entry topic_id (checked on insert)
 entry_id uuid NOT NULL REFERENCES runbook_entries(id) ON DELETE CASCADE,
 base_version integer NOT NULL CHECK (base_version > 0),
 patch jsonb NOT NULL CHECK (jsonb_typeof(patch) = 'object'),
   -- {section: {"claims":[Claim], "gap_ids":[uuid], "clears_open": bool}}
 gap_ids uuid[] NOT NULL,                   -- every seed gap id of this topic (leaders)
 status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','applied')),
 revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
 created_at timestamptz NOT NULL,
 applied_version integer,
 applied_sections jsonb,                    -- {section: "merge"|"replace"}
 applied_fingerprint text,
 applied_at timestamptz,
 applied_by_subject text,
 UNIQUE (session_id, topic_id),
 CHECK ((status = 'applied') = (applied_version IS NOT NULL AND applied_sections IS NOT NULL
        AND applied_fingerprint IS NOT NULL AND applied_at IS NOT NULL))
);
CREATE INDEX follow_up_proposals_entry ON follow_up_proposals (entry_id, topic_id);
-- Approval-time G7 lookup of open-section gap rows by entry.
CREATE INDEX follow_up_gaps_open_entry ON follow_up_gaps (entry_id) WHERE kind = 'open_section';
-- Applying a reviewed proposal is its own, honestly named entry decision.
ALTER TABLE entry_decisions DROP CONSTRAINT entry_decisions_action_check;
ALTER TABLE entry_decisions ADD CONSTRAINT entry_decisions_action_check CHECK
 (action IN ('approve','reject','edit','correction','review','withdraw','follow_up_apply'));
