-- Preserve author-owned voice extensions while retaining the legacy columns.
-- NULL identifies legacy rows; an explicit '{}' is a saved empty profile.
ALTER TABLE unified_characters ADD COLUMN voice_profile_json TEXT;
