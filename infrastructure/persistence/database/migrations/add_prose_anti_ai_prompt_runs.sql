CREATE TABLE IF NOT EXISTS anti_ai_prompt_runs (
    run_id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL DEFAULT '',
    chapter_number INTEGER,
    job_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_anti_ai_prompt_runs_novel ON anti_ai_prompt_runs(novel_id,created_at);
