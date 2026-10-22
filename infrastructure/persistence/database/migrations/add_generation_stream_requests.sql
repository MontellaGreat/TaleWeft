CREATE TABLE IF NOT EXISTS generation_stream_requests (
    request_id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    last_sequence INTEGER NOT NULL DEFAULT 0,
    terminal_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_generation_stream_updated ON generation_stream_requests(updated_at);
CREATE TABLE IF NOT EXISTS generation_stream_events (
    request_id TEXT NOT NULL REFERENCES generation_stream_requests(request_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    PRIMARY KEY(request_id,sequence)
);
