-- Durable outbox, inserted in the same transaction as the chapter revision.
CREATE TABLE IF NOT EXISTS chapter_derivation_jobs (
    job_id TEXT PRIMARY KEY,
    chapter_id TEXT NOT NULL,
    novel_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(chapter_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_chapter_derivation_jobs_pending
ON chapter_derivation_jobs(status, created_at);
