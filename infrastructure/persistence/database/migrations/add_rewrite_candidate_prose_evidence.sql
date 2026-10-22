-- A provider-generated candidate must never silently become a legacy candidate
-- when its mutable review audit is refreshed or damaged.
CREATE TRIGGER IF NOT EXISTS preserve_rewrite_candidate_prose_evidence
BEFORE UPDATE OF audit_json ON chapter_rewrite_candidates
WHEN json_extract(OLD.audit_json, '$.prose_evidence') IS NOT NULL
 AND json_extract(NEW.audit_json, '$.prose_evidence') IS NOT json_extract(OLD.audit_json, '$.prose_evidence')
BEGIN
    SELECT RAISE(ABORT, 'candidate prose evidence is immutable');
END;
