-- An entry the expert has edited by hand. Synthesis leaves such a draft alone unless it is
-- explicitly told to overwrite edits, so re-running it can no longer discard the expert's work.
ALTER TABLE runbook_entries ADD COLUMN edited boolean NOT NULL DEFAULT false;

-- Protect edits made before this flag existed: a draft with an edit in its history.
UPDATE runbook_entries e SET edited = true
WHERE e.status = 'draft'
  AND EXISTS (SELECT 1 FROM entry_decisions d WHERE d.entry_id = e.id AND d.action = 'edit');
