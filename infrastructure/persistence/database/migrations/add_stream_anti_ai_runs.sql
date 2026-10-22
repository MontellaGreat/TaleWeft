-- Runs after add_chapter_rewrite_candidates.sql on fresh databases.
ALTER TABLE anti_ai_rewrite_policies ADD COLUMN stream_action TEXT NOT NULL DEFAULT 'warn';
ALTER TABLE anti_ai_rewrite_policies ADD COLUMN stream_critical_limit INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS anti_ai_stream_runs (
    run_id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL DEFAULT '',
    chapter_number INTEGER,
    job_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    scan_status TEXT NOT NULL,
    persistence_status TEXT NOT NULL DEFAULT 'committed',
    content_hash TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'warn',
    summary_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_anti_ai_stream_runs_novel ON anti_ai_stream_runs(novel_id,created_at);
