CREATE TABLE IF NOT EXISTS anti_ai_scan_progress (
    run_id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    stage TEXT NOT NULL,
    scan_status TEXT NOT NULL,
    persistence_status TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    rule_version TEXT NOT NULL DEFAULT '',
    job_id TEXT NOT NULL DEFAULT '',
    run_epoch INTEGER,
    chapter_revision INTEGER,
    error TEXT,
    report_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_anti_ai_scan_progress_novel ON anti_ai_scan_progress(novel_id,updated_at);
