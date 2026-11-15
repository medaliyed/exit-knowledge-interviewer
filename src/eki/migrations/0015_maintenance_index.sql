-- Review dates and feedback update live metadata without spending another embedding call.
DROP TRIGGER runbook_index_invalidation ON runbook_entries;
CREATE TRIGGER runbook_index_invalidation AFTER UPDATE ON runbook_entries
FOR EACH ROW WHEN (
 OLD.version IS DISTINCT FROM NEW.version OR OLD.status IS DISTINCT FROM NEW.status
 OR OLD.content IS DISTINCT FROM NEW.content OR OLD.title IS DISTINCT FROM NEW.title
) EXECUTE FUNCTION invalidate_runbook_index();
