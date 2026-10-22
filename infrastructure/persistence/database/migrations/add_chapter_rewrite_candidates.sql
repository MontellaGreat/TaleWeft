-- Immutable rewrite bodies are independent of disposable generation previews.
CREATE TABLE IF NOT EXISTS chapter_rewrite_candidates (
    candidate_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    novel_id TEXT NOT NULL REFERENCES novels(id) ON DELETE RESTRICT,
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE RESTRICT,
    chapter_number INTEGER NOT NULL,
    kind TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    base_hash TEXT NOT NULL,
    base_revision INTEGER,
    base_content TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'generated',
    audit_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    committed_at TEXT,
    UNIQUE(run_id, kind, attempt)
);
CREATE INDEX IF NOT EXISTS ix_rewrite_candidate_chapter
    ON chapter_rewrite_candidates(novel_id, chapter_number, created_at);

CREATE TABLE IF NOT EXISTS anti_ai_rewrite_policies (
    novel_id TEXT PRIMARY KEY REFERENCES novels(id) ON DELETE RESTRICT,
    mode TEXT NOT NULL CHECK(mode IN ('scan_only', 'review', 'auto')),
    max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 5),
    max_severity REAL NOT NULL CHECK(max_severity BETWEEN 0 AND 100),
    updated_at TEXT NOT NULL
);
