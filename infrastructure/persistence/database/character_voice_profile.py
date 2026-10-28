"""Read full voice metadata together with the existing engine projections."""
import json


def read_character_voice_profile(row):
    raw = row.get('voice_profile_json')
    try:
        profile = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        profile = {}
    if not isinstance(profile, dict):
        profile = {}
    # The engine still updates these three scalar columns. Prefer their current
    # values over a stale copy in the full author profile, including clearing.
    for key, column in (('style', 'voice_style'), ('sentence_pattern', 'sentence_pattern'), ('speech_tempo', 'speech_tempo')):
        profile.pop(key, None)
        if row.get(column):
            profile[key] = row[column]
    return profile
