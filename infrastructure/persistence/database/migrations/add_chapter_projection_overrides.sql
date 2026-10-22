CREATE TABLE IF NOT EXISTS chapter_projection_overrides (
    novel_id TEXT NOT NULL,
    projection_table TEXT NOT NULL,
    row_id TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    PRIMARY KEY(novel_id,projection_table,row_id),
    FOREIGN KEY(novel_id) REFERENCES novels(id) ON DELETE CASCADE
);
