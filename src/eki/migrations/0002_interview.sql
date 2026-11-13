-- Phase 2 schema: the interview plan, the session, its transcript and what was learned.
-- Everything stored here has already been redacted. A confidential turn is stored and
-- flagged; code must never send it to a model, a trace or a runbook.

CREATE TABLE interview_plans (
    id          uuid PRIMARY KEY,
    run_id      uuid        NOT NULL REFERENCES ingest_runs (id) ON DELETE CASCADE,
    status      text        NOT NULL CHECK (status IN ('draft', 'approved')),
    approved_by text,
    approved_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CHECK (status = 'draft' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))
);

CREATE TABLE plan_topics (
    plan_id      uuid    NOT NULL REFERENCES interview_plans (id) ON DELETE CASCADE,
    position     integer NOT NULL,
    topic_id     text    NOT NULL,
    label        text    NOT NULL,
    kind         text    NOT NULL,
    system       text,
    origin       text    NOT NULL CHECK (origin IN ('ranked', 'contradiction', 'expert')),
    score        double precision,
    reason       text    NOT NULL,
    artifact_ids jsonb   NOT NULL,
    dropped      jsonb   NOT NULL,
    PRIMARY KEY (plan_id, topic_id),
    UNIQUE (plan_id, position)
);

CREATE TABLE questions (
    plan_id     uuid    NOT NULL,
    topic_id    text    NOT NULL,
    position    integer NOT NULL,
    text        text    NOT NULL,
    origin      text    NOT NULL CHECK (origin IN ('drafted', 'expert')),
    artifact_id text,
    quote       text,
    PRIMARY KEY (plan_id, topic_id, position),
    FOREIGN KEY (plan_id, topic_id) REFERENCES plan_topics (plan_id, topic_id) ON DELETE CASCADE,
    CHECK (origin = 'expert' OR (artifact_id IS NOT NULL AND quote IS NOT NULL))
);

CREATE TABLE sessions (
    id               uuid PRIMARY KEY,
    plan_id          uuid        NOT NULL UNIQUE REFERENCES interview_plans (id) ON DELETE CASCADE,
    status           text        NOT NULL CHECK (status IN ('active', 'finished')),
    current_topic_id text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz
);

CREATE TABLE turns (
    session_id   uuid        NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    ordinal      integer     NOT NULL,
    topic_id     text,
    speaker      text        NOT NULL CHECK (speaker IN ('interviewer', 'expert')),
    kind         text        NOT NULL CHECK (kind IN ('question', 'answer', 'skip', 'correction')),
    text         text        NOT NULL,
    confidential boolean     NOT NULL DEFAULT false,
    section      text,
    -- Interviewer turns: the workflow step that asked. A retried step rewrites its own turn.
    step         integer,
    -- Expert turns: the question answered. A retried answer rewrites its own turn.
    answers      integer,
    corrects     integer,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, ordinal)
);
CREATE UNIQUE INDEX turns_step_idx ON turns (session_id, step) WHERE step IS NOT NULL;
CREATE UNIQUE INDEX turns_answers_idx ON turns (session_id, answers) WHERE answers IS NOT NULL;

CREATE TABLE notes (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id   uuid    NOT NULL,
    topic_id     text    NOT NULL,
    section      text    NOT NULL CHECK (section IN
        ('trigger', 'diagnosis', 'steps', 'rollback', 'contacts', 'pitfalls', 'rationale')),
    text         text    NOT NULL,
    turn_ordinal integer NOT NULL,
    confidential boolean NOT NULL DEFAULT false,
    superseded   boolean NOT NULL DEFAULT false,
    FOREIGN KEY (session_id, turn_ordinal) REFERENCES turns (session_id, ordinal) ON DELETE CASCADE
);
-- At most one note in force per section of a topic.
CREATE UNIQUE INDEX notes_active_idx ON notes (session_id, topic_id, section) WHERE NOT superseded;

CREATE TABLE open_items (
    session_id   uuid NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    topic_id     text NOT NULL,
    section      text NOT NULL CHECK (section IN
        ('trigger', 'diagnosis', 'steps', 'rollback', 'contacts', 'pitfalls', 'rationale')),
    reason       text NOT NULL,
    turn_ordinal integer,
    PRIMARY KEY (session_id, topic_id, section)
);
