-- Durable audit trail for restoring legacy chapter_drafts into immutable content_versions.
CREATE TABLE IF NOT EXISTS chapter_draft_restore_audits (
    audit_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    novel_id TEXT NOT NULL,
    from_revision INTEGER NOT NULL,
    to_revision INTEGER NOT NULL,
    draft_content_hash TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'legacy_draft_restore',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- draft_id is retained as an immutable correlation value. Legacy drafts are
    -- intentionally trimmed after ten entries, so an FK would block retention.
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chapter_draft_restore_audits_chapter
    ON chapter_draft_restore_audits(chapter_id, to_revision DESC, created_at DESC);
