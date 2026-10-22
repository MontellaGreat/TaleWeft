CREATE TABLE IF NOT EXISTS autopilot_continuation_proposals (
    proposal_id TEXT PRIMARY KEY, novel_id TEXT NOT NULL, mode TEXT NOT NULL,
    base_stage TEXT NOT NULL, base_target_chapters INTEGER NOT NULL,
    proposed_target_chapters INTEGER NOT NULL, completed_chapters INTEGER NOT NULL DEFAULT 0,
    from_chapter INTEGER NOT NULL, rationale TEXT NOT NULL DEFAULT '', plan_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed', command_id TEXT, created_at TEXT NOT NULL,
    approved_at TEXT, rejected_at TEXT, base_run_epoch INTEGER NOT NULL DEFAULT 0,
    run_epoch INTEGER, structure_change_id TEXT, structure_json TEXT, structure_confirmed_at TEXT,
    UNIQUE(novel_id,proposal_id)
);
-- Existing builds created the table lazily; migrate each field independently.
ALTER TABLE autopilot_continuation_proposals ADD COLUMN base_run_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE autopilot_continuation_proposals ADD COLUMN run_epoch INTEGER;
ALTER TABLE autopilot_continuation_proposals ADD COLUMN structure_change_id TEXT;
ALTER TABLE autopilot_continuation_proposals ADD COLUMN structure_json TEXT;
ALTER TABLE autopilot_continuation_proposals ADD COLUMN structure_confirmed_at TEXT;
UPDATE autopilot_continuation_proposals SET status='legacy_unverified'
WHERE status='applied' AND structure_confirmed_at IS NULL;
