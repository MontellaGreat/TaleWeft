-- Capture all write paths, including legacy direct SQL. History survives deletion.
CREATE TABLE IF NOT EXISTS content_versions (
    version_id TEXT PRIMARY KEY,
    chapter_id TEXT NOT NULL,
    novel_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    content TEXT NOT NULL,
    outline TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    content_hash TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'legacy',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(chapter_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_content_versions_chapter ON content_versions(chapter_id, revision DESC);

CREATE TRIGGER IF NOT EXISTS chapters_initial_version
AFTER INSERT ON chapters BEGIN
    INSERT OR IGNORE INTO content_versions
      (version_id,chapter_id,novel_id,chapter_number,revision,content,outline,title,status,content_hash,source)
    VALUES (lower(hex(randomblob(16))),NEW.id,NEW.novel_id,NEW.number,NEW.revision,
            COALESCE(NEW.content,''),COALESCE(NEW.outline,''),COALESCE(NEW.title,''),NEW.status,NEW.content_hash,
            CASE WHEN NEW.version_source='' THEN 'initial' ELSE NEW.version_source END);
END;

CREATE TRIGGER IF NOT EXISTS chapters_content_version
AFTER UPDATE OF content,outline,title,status,generation_hint ON chapters
WHEN OLD.content IS NOT NEW.content OR OLD.outline IS NOT NEW.outline OR OLD.title IS NOT NEW.title
  OR OLD.status IS NOT NEW.status OR OLD.generation_hint IS NOT NEW.generation_hint OR NEW.revision>OLD.revision
BEGIN
    INSERT OR IGNORE INTO content_versions
      (version_id,chapter_id,novel_id,chapter_number,revision,content,outline,title,status,content_hash,source)
    VALUES (lower(hex(randomblob(16))),OLD.id,OLD.novel_id,OLD.number,OLD.revision,
            COALESCE(OLD.content,''),COALESCE(OLD.outline,''),COALESCE(OLD.title,''),OLD.status,OLD.content_hash,'baseline');
    UPDATE chapters SET revision=MAX(OLD.revision+1,NEW.revision),
      content_hash=CASE WHEN OLD.content IS NOT NEW.content AND NEW.content_hash=OLD.content_hash THEN '' ELSE NEW.content_hash END,
      version_source='' WHERE id=NEW.id;
    INSERT OR IGNORE INTO content_versions
      (version_id,chapter_id,novel_id,chapter_number,revision,content,outline,title,status,content_hash,source)
    SELECT lower(hex(randomblob(16))),id,novel_id,number,revision,COALESCE(content,''),COALESCE(outline,''),
           COALESCE(title,''),status,content_hash,CASE WHEN NEW.version_source='' THEN 'legacy' ELSE NEW.version_source END
      FROM chapters WHERE id=NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS chapters_deleted_version BEFORE DELETE ON chapters BEGIN
    INSERT OR IGNORE INTO content_versions
      (version_id,chapter_id,novel_id,chapter_number,revision,content,outline,title,status,content_hash,source)
    VALUES (lower(hex(randomblob(16))),OLD.id,OLD.novel_id,OLD.number,OLD.revision,
            COALESCE(OLD.content,''),COALESCE(OLD.outline,''),COALESCE(OLD.title,''),OLD.status,OLD.content_hash,'before_delete');
END;
CREATE TRIGGER IF NOT EXISTS content_versions_immutable_update BEFORE UPDATE ON content_versions BEGIN
    SELECT RAISE(ABORT,'content versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS content_versions_immutable_delete BEFORE DELETE ON content_versions BEGIN
    SELECT RAISE(ABORT,'content versions are immutable');
END;
