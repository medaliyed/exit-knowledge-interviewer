-- A confirmation must refer to an explicit review after this report, even under a frozen clock.
ALTER TABLE entry_flags ADD COLUMN decision_cursor bigint NOT NULL DEFAULT 0;
CREATE FUNCTION refuse_resolved_flag_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.resolution IS NOT NULL THEN
  RAISE EXCEPTION 'resolved flags are immutable' USING ERRCODE='55000';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER resolved_flags_immutable BEFORE UPDATE ON entry_flags
 FOR EACH ROW EXECUTE FUNCTION refuse_resolved_flag_change();
