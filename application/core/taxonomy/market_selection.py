"""Validate project market selections and freeze the effective writing profile."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Mapping

from .cn_market_v2 import (
    CHANNEL_IDS, PROFILE_DIMENSIONS, TaxonomyV2ValidationError,
    load_cn_market_v2_bundle, missing_profile_dimensions,
    resolve_theme_profile_for_generation, validate_custom_market_profile,
)

PROFILE_FIELDS = {
    "world_tone": "locked_world_preset",
    "story_structure": "locked_story_structure",
    "pacing_control": "locked_pacing_control",
    "writing_style": "locked_writing_style",
    "special_requirements": "locked_special_requirements",
}
MARKET_FIELDS = (
    "audience_mode", "canonical_genre_id", "canonical_theme_id", "market_platform",
    "market_category_path", "market_tags", "taxonomy_version", "profile_version",
    "reader_contract", "custom_market_profile",
)


def profile_snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def normalize_market_preferences(raw: Mapping[str, Any], *, for_generation: bool = False) -> dict[str, Any]:
    """Return validated values, deriving the snapshot/hash on the server.

    Legacy books without stable market IDs remain readable. New selections must
    use the current registry and matching parents/channel. Saving an internal
    draft does not approve it: generation requires registry review or an explicit
    project override that confirms the final five dimensions.
    """
    result = deepcopy(dict(raw))
    for key in ("audience_mode", "canonical_genre_id", "canonical_theme_id", "taxonomy_version", "profile_version", "market_platform", "reader_contract"):
        value = result.get(key, "")
        if not isinstance(value, str):
            raise TaxonomyV2ValidationError(f"{key} must be a string")
        result[key] = value.strip()
    for key in ("market_category_path", "market_tags"):
        values = result.get(key, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise TaxonomyV2ValidationError(f"{key} must be a list of non-empty strings")
        result[key] = [value.strip() for value in values]
    custom = result.get("custom_market_profile", {})
    if not isinstance(custom, dict):
        raise TaxonomyV2ValidationError("custom_market_profile must be an object")
    result["custom_market_profile"] = custom
    category_id = result["canonical_genre_id"]
    theme_id = result["canonical_theme_id"]
    structured = bool(category_id or theme_id or custom)
    old_snapshot = result.get("market_profile_snapshot")
    if not structured and isinstance(old_snapshot, dict) and any(old_snapshot.get(key) for key in ("canonical_genre_id", "canonical_theme_id", "custom_market_profile")):
        raise TaxonomyV2ValidationError("structured market preferences cannot fall back to legacy")
    profile = {}
    for dimension, key in PROFILE_FIELDS.items():
        value = result.get(key, "")
        if not isinstance(value, str):
            raise TaxonomyV2ValidationError(f"{key} must be a string")
        profile[dimension] = value.strip()
    status = "legacy_unreviewed"
    if structured:
        bundle = load_cn_market_v2_bundle()
        audience = result["audience_mode"]
        if audience not in CHANNEL_IDS:
            raise TaxonomyV2ValidationError("audience_mode must be male, female, or dual")
        supplied_version = result["taxonomy_version"]
        if supplied_version and supplied_version != bundle["taxonomy_version"]:
            raise TaxonomyV2ValidationError("taxonomy_version is not the current registry version")
        result["taxonomy_version"] = bundle["taxonomy_version"]
        categories = {item["id"]: item for item in bundle["categories"]}
        themes = {item["id"]: item for item in bundle["themes"]}
        category = categories.get(category_id) if category_id else None
        theme = themes.get(theme_id) if theme_id else None
        if category_id and category is None:
            raise TaxonomyV2ValidationError(f"unknown canonical_genre_id: {category_id}")
        if theme_id and theme is None:
            raise TaxonomyV2ValidationError(f"unknown canonical_theme_id: {theme_id}")
        if theme and (not category or theme["category_id"] != category_id):
            raise TaxonomyV2ValidationError("canonical category/theme parents do not match")
        if category and audience not in category["audience_modes"]:
            raise TaxonomyV2ValidationError("canonical category does not support the selected audience")
        if theme and audience not in theme["audience_modes"]:
            raise TaxonomyV2ValidationError("canonical theme does not support the selected audience")
        if custom:
            validate_custom_market_profile(custom, for_generation=True)
            if custom["audience_mode"] != audience:
                raise TaxonomyV2ValidationError("custom audience must match audience_mode")
            custom_category = custom.get("custom_category")
            custom_theme = custom.get("custom_theme")
            if not isinstance(custom_theme, dict):
                raise TaxonomyV2ValidationError("a custom category requires a custom theme")
            if custom_category and category_id:
                raise TaxonomyV2ValidationError("a custom category cannot also select a canonical category")
            if custom_theme.get("parent_canonical_category_id") != (category_id or None):
                raise TaxonomyV2ValidationError("custom theme canonical parent must match canonical_genre_id")
            source_id = custom.get("source_canonical_theme_id") or custom_theme.get("source_canonical_theme_id") or ""
            if custom.get("source_canonical_theme_id") and custom_theme.get("source_canonical_theme_id") and custom["source_canonical_theme_id"] != custom_theme["source_canonical_theme_id"]:
                raise TaxonomyV2ValidationError("custom theme source IDs do not match")
            if source_id != theme_id:
                raise TaxonomyV2ValidationError("custom source theme must match canonical_theme_id")
            custom_profile = custom_theme["profile"]
            for dimension in PROFILE_DIMENSIONS:
                value = custom_profile[dimension].strip()
                if profile[dimension] and profile[dimension] != value:
                    raise TaxonomyV2ValidationError(f"custom profile conflicts with final {dimension}")
                profile[dimension] = value
                custom_profile[dimension] = value
            confirmed = (custom.get("profile_review_status") == "user_confirmed"
                         and custom_theme.get("status") == "user_confirmed")
            status = "user_confirmed" if confirmed else "custom_profile_draft"
            if for_generation and not confirmed:
                raise TaxonomyV2ValidationError("custom profile requires explicit user confirmation")
            result["profile_version"] = result["profile_version"] or "project_profile_v1"
        else:
            if not theme:
                raise TaxonomyV2ValidationError("canonical category requires a canonical or custom theme")
            base = dict(theme["profile"])
            overlay = theme.get("audience_profile_overlays", {}).get(audience, {})
            if overlay.get("status") == "defined":
                base.update(overlay.get("profile", {}))
            for dimension in PROFILE_DIMENSIONS:
                profile[dimension] = profile[dimension] or base[dimension]
            status = theme["status"]
            if for_generation:
                reviewed = resolve_theme_profile_for_generation(bundle, theme_id, audience)
                if profile != reviewed:
                    raise TaxonomyV2ValidationError("edited builtin profile requires a confirmed project override")
            result["profile_version"] = result["profile_version"] or str(theme.get("profile_version") or theme.get("profile_ref") or "")
        missing = missing_profile_dimensions(profile)
        if missing:
            raise TaxonomyV2ValidationError("market profile is incomplete: " + ", ".join(missing))
        selected_category = category_id or custom.get("custom_category", {}).get("id", "")
        selected_theme = theme_id or custom.get("custom_theme", {}).get("id", "")
        expected_path = [audience, selected_category, selected_theme]
        if result["market_category_path"] and result["market_category_path"] != expected_path:
            raise TaxonomyV2ValidationError("market_category_path does not match the selected stable IDs")
        result["market_category_path"] = expected_path
    for dimension, key in PROFILE_FIELDS.items():
        result[key] = profile[dimension]
    snapshot = {key: deepcopy(result.get(key, [] if key in {"market_category_path", "market_tags"} else {} if key == "custom_market_profile" else "")) for key in MARKET_FIELDS}
    snapshot.update(profile=profile, profile_status=status)
    result["market_profile_snapshot"] = snapshot
    result["profile_snapshot_hash"] = profile_snapshot_hash(snapshot)
    return result


def validate_market_preferences_for_generation(preferences: Any) -> dict[str, Any]:
    """Validate persisted preferences, including snapshot integrity, at runtime."""
    raw = preferences.to_dict() if hasattr(preferences, "to_dict") else dict(preferences or {})
    normalized = normalize_market_preferences(raw, for_generation=True)
    if raw.get("canonical_genre_id") or raw.get("canonical_theme_id") or raw.get("custom_market_profile"):
        snapshot = raw.get("market_profile_snapshot")
        if snapshot != normalized["market_profile_snapshot"] or raw.get("profile_snapshot_hash") != normalized["profile_snapshot_hash"]:
            raise TaxonomyV2ValidationError("market profile snapshot does not match current preferences")
    elif isinstance(raw.get("market_profile_snapshot"), dict):
        snapshot = raw["market_profile_snapshot"]
        if snapshot.get("canonical_genre_id") or snapshot.get("canonical_theme_id") or snapshot.get("custom_market_profile"):
            raise TaxonomyV2ValidationError("structured market preferences cannot fall back to legacy using an existing snapshot")
    return normalized["market_profile_snapshot"]
