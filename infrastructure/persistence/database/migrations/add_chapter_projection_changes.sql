-- Field provenance permits undoing generated projections while retaining author edits.
CREATE TABLE IF NOT EXISTS chapter_projection_changes (
    novel_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    projection_table TEXT NOT NULL,
    row_id TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    PRIMARY KEY(novel_id,chapter_number,projection_table,row_id),
    FOREIGN KEY(novel_id) REFERENCES novels(id) ON DELETE CASCADE
);
