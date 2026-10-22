ALTER TABLE chapter_derivation_jobs ADD COLUMN cascade_status TEXT NOT NULL DEFAULT 'not_requested';
ALTER TABLE chapter_derivation_jobs ADD COLUMN cascade_error TEXT NOT NULL DEFAULT '';
