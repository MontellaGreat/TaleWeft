CREATE TABLE IF NOT EXISTS manuscript_imports (
    preview_id TEXT PRIMARY KEY,
    new_novel_id TEXT NOT NULL UNIQUE,
    document_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','imported')),
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    receipt_json TEXT
);
CREATE TRIGGER IF NOT EXISTS manuscript_preview_immutable
BEFORE UPDATE OF new_novel_id,document_json,payload_sha256,validation_json,created_at,expires_at ON manuscript_imports
BEGIN SELECT RAISE(ABORT, 'manuscript preview is immutable'); END;
