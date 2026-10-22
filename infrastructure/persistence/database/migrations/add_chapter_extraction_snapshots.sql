-- Previous AI extraction evidence allows replacing obsolete chapter artifacts.
CREATE TABLE IF NOT EXISTS chapter_extraction_snapshots (
    novel_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    bundle_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(novel_id, chapter_number),
    FOREIGN KEY(novel_id) REFERENCES novels(id) ON DELETE CASCADE
);
