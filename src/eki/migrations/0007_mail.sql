-- Consent selection is fixed per ingestion snapshot; default remains no mail.
ALTER TABLE ingest_runs ADD COLUMN mail_folders jsonb NOT NULL DEFAULT '[]';
