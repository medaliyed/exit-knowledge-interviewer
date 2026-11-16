-- F22: follow-up plans with a parent session, selected section scope, attributed seed
-- questions, the recorded respondent, durable session-wide budgets and per-seed outcomes.
ALTER TABLE interview_plans ADD COLUMN kind text NOT NULL DEFAULT 'capture' CHECK (kind IN ('capture','follow_up'));
ALTER TABLE interview_plans ADD COLUMN parent_session_id uuid REFERENCES sessions(id) ON DELETE SET NULL;
ALTER TABLE interview_plans ADD COLUMN limits jsonb;
ALTER TABLE interview_plans ADD COLUMN revision integer NOT NULL DEFAULT 1;
ALTER TABLE interview_plans ADD COLUMN request_fingerprint text;
ALTER TABLE plan_topics DROP CONSTRAINT plan_topics_origin_check;
ALTER TABLE plan_topics ADD CONSTRAINT plan_topics_origin_check
 CHECK (origin IN ('ranked','contradiction','expert','follow_up'));
ALTER TABLE plan_topics ADD COLUMN sections jsonb;
ALTER TABLE plan_topics ADD COLUMN target_entry_id uuid REFERENCES runbook_entries(id) ON DELETE SET NULL;
ALTER TABLE plan_topics ADD COLUMN base_version integer;
ALTER TABLE questions DROP CONSTRAINT questions_origin_check;
ALTER TABLE questions ADD CONSTRAINT questions_origin_check CHECK (origin IN ('drafted','expert','follow_up'));
ALTER TABLE questions DROP CONSTRAINT questions_check;
ALTER TABLE questions ADD CONSTRAINT questions_check
 CHECK (origin <> 'drafted' OR (artifact_id IS NOT NULL AND quote IS NOT NULL));
ALTER TABLE questions ADD COLUMN gap_id uuid;
ALTER TABLE questions ADD COLUMN section text;
ALTER TABLE questions ADD CONSTRAINT questions_follow_up_gap CHECK ((origin = 'follow_up') = (gap_id IS NOT NULL));
ALTER TABLE turns ADD COLUMN gap_id uuid;
ALTER TABLE turns ADD COLUMN follow_up_outcome text CHECK (follow_up_outcome = 'unknown');
ALTER TABLE sessions ADD COLUMN respondent_subject text;
ALTER TABLE sessions ADD COLUMN completion_reason text
 CHECK (completion_reason IN ('complete','question_limit','model_budget','expert_stopped'));
CREATE TABLE session_usage (
 session_id uuid PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
 model_calls integer NOT NULL DEFAULT 0,
 embedding_calls integer NOT NULL DEFAULT 0
);
CREATE TABLE session_provider_attempts (
 call_id uuid PRIMARY KEY,
 session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 counter text NOT NULL CHECK (counter IN ('model_calls','embedding_calls'))
);
-- Follow-up answers retain independent facts in one section, each with its own turn.
DROP INDEX notes_active_idx;
CREATE UNIQUE INDEX notes_active_idx ON notes (session_id, topic_id, section, turn_ordinal)
 WHERE NOT superseded;
CREATE TABLE follow_up_attempts (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 gap_id uuid NOT NULL REFERENCES follow_up_gaps(id) ON DELETE CASCADE,
 session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 outcome text NOT NULL CHECK (outcome IN ('answered','answered_unnoted','unknown','skipped','confidential','unattempted')),
 question_turn integer,
 recorded_at timestamptz NOT NULL,
 UNIQUE (gap_id, session_id)
);
ALTER TABLE follow_up_gaps ADD COLUMN plan_id uuid REFERENCES interview_plans(id) ON DELETE SET NULL;
