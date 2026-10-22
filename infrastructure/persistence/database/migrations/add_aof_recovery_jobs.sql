-- Durable recovery ledger for append-only chapter drafts.
-- The file remains the source of truth until the DB content is verified and
-- the exact unchanged snapshot is deleted.
CREATE TABLE IF NOT EXISTS aof_recovery_jobs (
    job_id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    draft_path TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    job_type TEXT NOT NULL DEFAULT 'recover',
    repair_resume_state INTEGER NOT NULL DEFAULT 0,
    committed_revision INTEGER,
    committed_content_hash TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    UNIQUE(novel_id, chapter_number, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_aof_recovery_jobs_status
    ON aof_recovery_jobs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_aof_recovery_jobs_novel
    ON aof_recovery_jobs(novel_id, chapter_number, updated_at DESC);
