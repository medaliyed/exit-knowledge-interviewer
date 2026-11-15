-- F19: email/password accounts, server-side sessions, one-time links and sign-in throttling.
-- Only hashes of passwords, session IDs and link tokens are stored. Accounts are disabled,
-- never deleted, so audit user_id values stay resolvable.
CREATE TABLE accounts (
    id uuid PRIMARY KEY,
    subject text NOT NULL UNIQUE CHECK (length(btrim(subject)) > 0 AND length(subject) <= 200),
    email text NOT NULL UNIQUE CHECK (email = lower(btrim(email)) AND length(email) <= 254),
    name text NOT NULL CHECK (length(btrim(name)) > 0),
    admin boolean NOT NULL DEFAULT false,
    password_hash text,
    disabled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL,
    password_changed_at timestamptz,
    last_sign_in_at timestamptz
);
CREATE TABLE auth_sessions (
    id_hash bytea PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    user_agent text NOT NULL DEFAULT '' CHECK (length(user_agent) <= 200)
);
CREATE INDEX auth_sessions_account_idx ON auth_sessions (account_id, expires_at);
CREATE INDEX auth_sessions_expiry_idx ON auth_sessions (expires_at);
CREATE TABLE auth_links (
    token_hash bytea PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    purpose text NOT NULL CHECK (purpose IN ('invite', 'reset')),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    used_at timestamptz
);
CREATE INDEX auth_links_account_idx ON auth_links (account_id);
CREATE UNIQUE INDEX auth_links_unused_account_idx ON auth_links (account_id) WHERE used_at IS NULL;
-- Short-lived failure counters. Keys are an email hash or a client address; pruned after a day.
CREATE TABLE auth_throttle (
    key text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('email', 'ip')),
    failed_at timestamptz NOT NULL
);
CREATE INDEX auth_throttle_key_idx ON auth_throttle (kind, key, failed_at);
CREATE INDEX auth_throttle_retention_idx ON auth_throttle (failed_at);
ALTER TABLE audit_events DROP CONSTRAINT audit_events_kind_check;
ALTER TABLE audit_events ADD CONSTRAINT audit_events_kind_check
    CHECK (kind IN ('model','tool','approval','review','access','ingest','delivery','generation','auth'));
