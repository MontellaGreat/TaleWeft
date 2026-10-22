CREATE TABLE IF NOT EXISTS data_change_proposals (
    proposal_id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    base_hash TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    after_json TEXT,
    after_hash TEXT,
    run_epoch INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    applied_at TEXT,
    rolled_back_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_data_change_novel ON data_change_proposals(novel_id,created_at);
CREATE TABLE IF NOT EXISTS data_change_audits (
    audit_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    novel_id TEXT NOT NULL,
    event TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS data_change_snapshot_immutable
BEFORE UPDATE OF novel_id,operation,proposal_json,proposal_hash,base_hash,snapshot_id,snapshot_json,run_epoch ON data_change_proposals
BEGIN SELECT RAISE(ABORT,'data change proposal and snapshot are immutable'); END;
