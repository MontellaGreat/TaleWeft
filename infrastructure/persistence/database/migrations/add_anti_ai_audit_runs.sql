CREATE TABLE IF NOT EXISTS anti_ai_audit_runs (
    run_id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    scan_status TEXT NOT NULL,
    persistence_status TEXT NOT NULL,
    error TEXT,
    report_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_anti_ai_runs_chapter
ON anti_ai_audit_runs(novel_id, chapter_number, created_at);
