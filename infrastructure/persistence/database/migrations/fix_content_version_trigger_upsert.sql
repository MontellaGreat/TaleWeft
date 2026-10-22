-- An outer UPSERT overrides OR IGNORE inside its trigger. Avoid collisions
-- by selecting only missing versions instead of relying on a conflict policy.
DROP TRIGGER IF EXISTS chapters_initial_version;
DROP TRIGGER IF EXISTS chapters_content_version;
DROP TRIGGER IF EXISTS chapters_deleted_version;

CREATE TRIGGER chapters_initial_version AFTER INSERT ON chapters BEGIN
    UPDATE chapters SET revision=MAX(NEW.revision,
        COALESCE((SELECT MAX(revision)+1 FROM content_versions WHERE chapter_id=NEW.id),0))
        WHERE id=NEW.id;
    INSERT INTO content_versions
      (version_id,chapter_id,novel_id,chapter_number,revision,content,outline,title,status,content_hash,source)
    SELECT lower(hex(randomblob(16))),id,novel_id,number,revision,COALESCE(content,''),
           COALESCE(outline,''),COALESCE(title,''),status,content_hash,
           CASE WHEN version_source='' THEN 'initial' ELSE version_source END
      FROM chapters WHERE id=NEW.id AND NOT EXISTS
        (SELECT 1 FROM content_versions WHERE chapter_id=NEW.id AND revision=chapters.revision);
END;

CREATE TRIGGER chapters_content_version
AFTER UPDATE OF content,outline,title,status,generation_hint ON chapters
WHEN OLD.content IS NOT NEW.content OR OLD.outline IS NOT NEW.outline OR OLD.title IS NOT NEW.title
  OR OLD.status IS NOT NEW.status OR OLD.generation_hint IS NOT NEW.generation_hint OR NEW.revision>OLD.revision
BEGIN
    INSERT INTO content_versions
      (version_id,chapter_id,novel_id,chapter_number,revision,content,outline,title,status,content_hash,source)
    SELECT lower(hex(randomblob(16))),OLD.id,OLD.novel_id,OLD.number,OLD.revision,
           COALESCE(OLD.content,''),COALESCE(OLD.outline,''),COALESCE(OLD.title,''),OLD.status,OLD.content_hash,'baseline'
      WHERE NOT EXISTS(SELECT 1 FROM content_versions WHERE chapter_id=OLD.id AND revision=OLD.revision);
    UPDATE chapters SET revision=MAX(OLD.revision+1,NEW.revision),
      content_hash=CASE WHEN OLD.content IS NOT NEW.content AND NEW.content_hash=OLD.content_hash THEN '' ELSE NEW.content_hash END,
      version_source='' WHERE id=NEW.id;
    INSERT INTO content_versions
      (version_id,chapter_id,novel_id,chapter_number,revision,content,outline,title,status,content_hash,source)
    SELECT lower(hex(randomblob(16))),id,novel_id,number,revision,COALESCE(content,''),COALESCE(outline,''),
           COALESCE(title,''),status,content_hash,CASE WHEN NEW.version_source='' THEN 'legacy' ELSE NEW.version_source END
      FROM chapters WHERE id=NEW.id AND NOT EXISTS
        (SELECT 1 FROM content_versions WHERE chapter_id=NEW.id AND revision=chapters.revision);
END;

CREATE TRIGGER chapters_deleted_version BEFORE DELETE ON chapters BEGIN
    INSERT INTO content_versions
      (version_id,chapter_id,novel_id,chapter_number,revision,content,outline,title,status,content_hash,source)
    SELECT lower(hex(randomblob(16))),OLD.id,OLD.novel_id,OLD.number,OLD.revision,
           COALESCE(OLD.content,''),COALESCE(OLD.outline,''),COALESCE(OLD.title,''),OLD.status,OLD.content_hash,'before_delete'
      WHERE NOT EXISTS(SELECT 1 FROM content_versions WHERE chapter_id=OLD.id AND revision=OLD.revision);
END;
