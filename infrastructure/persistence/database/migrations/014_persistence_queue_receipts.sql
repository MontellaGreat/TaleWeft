-- Durable persistence queue receipts (P1-2).
-- Results and commit sequence are retained so a restarted API process can
-- reconcile an accepted command without relying on an in-memory ACK pipe.
ALTER TABLE persistence_queue ADD COLUMN idempotency_key TEXT;
ALTER TABLE persistence_queue ADD COLUMN result_json TEXT;
ALTER TABLE persistence_queue ADD COLUMN commit_lsn INTEGER;

CREATE UNIQUE INDEX IF NOT EXISTS idx_persistence_queue_idempotency
ON persistence_queue(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS persistence_queue_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_lsn INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO persistence_queue_meta(id, next_lsn) VALUES (1, 0);
