CREATE TABLE IF NOT EXISTS chapter_save_receipts (
    request_id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','committed','failed')),
    chapter_id TEXT,
    revision INTEGER,
    content_hash TEXT,
    derivation_job_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chapter_save_receipts_scope
ON chapter_save_receipts(novel_id,chapter_number,created_at);
