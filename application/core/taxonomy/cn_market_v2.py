"""Load and validate the versioned Chinese web-novel market taxonomy."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from datetime import date
from typing import Any, Dict, Mapping

import yaml


PROFILE_DIMENSIONS = (
    "world_tone",
    "story_structure",
    "pacing_control",
    "writing_style",
    "special_requirements",
)
CHANNEL_IDS = {"male", "female", "dual"}
PROFILE_READY_STATUSES = {"ready"}
PROFILE_COMPLETE_STATUSES = {"ready", "draft_profile_complete", "legacy_review_required"}
OVERLAY_STATUSES = {"defined", "no_difference", "pending_review"}
THEME_STATUSES = PROFILE_COMPLETE_STATUSES | {"planned", "draft_split_required"}
CATEGORY_STATUSES = {"legacy_compatible", "draft_catalog", "ready"}
MAPPING_STATUSES = {
    "mapped_category",
    "mapped_theme",
    "mapped_alias",
    "mapped_legacy_alias",
    "parent_only_pending_theme",
    "pending_mapping",
    "ambiguous",
}
RESOLVED_MAPPING_STATUSES = {"mapped_category", "mapped_theme", "mapped_alias", "mapped_legacy_alias"}
PLATFORM_STATUSES = {"observed", "pending_source_review"}


class TaxonomyV2ValidationError(ValueError):
    """Raised when the built-in taxonomy violates its machine-readable contract."""


def taxonomy_v2_yaml_path() -> Path:
    """Resolve the v2 YAML from any working directory."""
    return Path(__file__).resolve().parents[3] / "shared" / "taxonomy" / "builtin_cn_v2.yaml"


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _string_is_one_of(value: Any, allowed: set[str]) -> bool:
    return isinstance(value, str) and value in allowed


def _records_by_id(records: Any, label: str, errors: list[str]) -> dict[str, Mapping[str, Any]]:
    if not isinstance(records, list):
        errors.append(f"{label} must be a list")
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            errors.append(f"{label}[{index}] must be an object")
            continue
        identifier = record.get("id")
        if not _non_empty_string(identifier):
            errors.append(f"{label}[{index}].id must be a non-empty string")
            continue
        if identifier in result:
            errors.append(f"duplicate {label} id: {identifier}")
        result[identifier] = record
    return result


def _require_references(
    references: Any,
    targets: Mapping[str, Any],
    field_name: str,
    owner_id: str,
    errors: list[str],
) -> list[str]:
    if not isinstance(references, list) or any(not _non_empty_string(item) for item in references):
        errors.append(f"{field_name} for {owner_id} must be a list of ids")
        return []
    if len(references) != len(set(references)):
        errors.append(f"{field_name} for {owner_id} contains duplicate ids")
    for reference in references:
        if reference not in targets:
            errors.append(f"{field_name} for {owner_id} references unknown id {reference}")
    return references


def _validate_profile(profile: Any, owner: str, errors: list[str], *, required: bool) -> None:
    if profile is None and not required:
        return
    if not isinstance(profile, dict):
        errors.append(f"{owner} must be a profile object" if required else f"{owner} must be an object or null")
        return
    if set(profile) != set(PROFILE_DIMENSIONS):
        errors.append(f"{owner} must contain exactly the five dimensions")
    for dimension in PROFILE_DIMENSIONS:
        if not _non_empty_string(profile.get(dimension)):
            errors.append(f"{owner}.{dimension} is incomplete")


def _validate_source_metadata(source: Any, owner: str, errors: list[str]) -> None:
    if not isinstance(source, dict):
        errors.append(f"{owner} must be an object")
        return
    for field_name in ("type", "file"):
        if not _non_empty_string(source.get(field_name)):
            errors.append(f"{owner}.{field_name} must be non-empty")


def validate_cn_market_v2_bundle(bundle: Any) -> None:
    """Validate IDs, parent links, channel references, profile readiness and sources."""
    errors: list[str] = []
    if not isinstance(bundle, dict):
        raise TaxonomyV2ValidationError("taxonomy bundle must be an object")
    if bundle.get("schema_kind") != "plotpilot.cn_market_taxonomy":
        errors.append("schema_kind must be plotpilot.cn_market_taxonomy")
    if bundle.get("schema_version") != 2:
        errors.append("schema_version must be 2")
    if not _non_empty_string(bundle.get("taxonomy_version")):
        errors.append("taxonomy_version must be a non-empty string")
    if not _non_empty_string(bundle.get("taxonomy_id")):
        errors.append("taxonomy_id must be a non-empty string")
    if not _non_empty_string(bundle.get("locale")):
        errors.append("locale must be a non-empty string")
    if not _non_empty_string(bundle.get("title")):
        errors.append("title must be a non-empty string")
    if bundle.get("profile_dimensions") != list(PROFILE_DIMENSIONS):
        errors.append("profile_dimensions must match the five-dimension contract in order")

    channels = _records_by_id(bundle.get("channels"), "channel", errors)
    categories = _records_by_id(bundle.get("categories"), "category", errors)
    themes = _records_by_id(bundle.get("themes"), "theme", errors)
    canonical_ids = set(categories) | set(themes)
    if len(canonical_ids) != len(categories) + len(themes):
        errors.append("canonical category and theme ids must be globally unique")
    if set(channels) != CHANNEL_IDS:
        errors.append("channels must define exactly male, female, and dual")

    for channel_id, channel in channels.items():
        if not isinstance(channel.get("labels"), dict) or not _non_empty_string(channel.get("labels", {}).get("zh-CN")):
            errors.append(f"channel {channel_id} requires labels.zh-CN")
        if not _non_empty_string(channel.get("description")):
            errors.append(f"channel {channel_id}.description must be non-empty")

    category_legacy_ids: set[str] = set()
    theme_memberships: dict[str, list[str]] = {}
    for category_id, category in categories.items():
        if not category_id.startswith("major."):
            errors.append(f"canonical category id must use major.*: {category_id}")
        if not isinstance(category.get("labels"), dict) or not _non_empty_string(category.get("labels", {}).get("zh-CN")):
            errors.append(f"category {category_id} requires labels.zh-CN")
        modes = category.get("audience_modes")
        valid_modes = isinstance(modes, list) and all(isinstance(mode, str) and mode in CHANNEL_IDS for mode in modes)
        if not valid_modes or not modes or len(modes) != len(set(modes) if valid_modes else []):
            errors.append(f"category {category_id} has invalid audience_modes")
        if not _string_is_one_of(category.get("status"), CATEGORY_STATUSES):
            errors.append(f"category {category_id} has invalid status {category.get('status')}")
        default_status = category.get("default_profile_status")
        if default_status is not None and not _string_is_one_of(default_status, PROFILE_COMPLETE_STATUSES | {"planned"}):
            errors.append(f"category {category_id} has invalid default_profile_status {default_status}")
        default_profile = category.get("default_profile")
        if default_status == "ready":
            if category.get("status") != "ready":
                errors.append(f"category {category_id} ready default profile requires ready category status")
            _validate_profile(default_profile, f"category {category_id}.default_profile", errors, required=True)
        elif default_profile is not None:
            _validate_profile(default_profile, f"category {category_id}.default_profile", errors, required=False)
        legacy_ids = category.get("legacy_ids", [])
        if not isinstance(legacy_ids, list) or any(not _non_empty_string(item) for item in legacy_ids):
            errors.append(f"category {category_id}.legacy_ids must be strings")
        else:
            for legacy_id in legacy_ids:
                if legacy_id in category_legacy_ids:
                    errors.append(f"duplicate category legacy id: {legacy_id}")
                category_legacy_ids.add(legacy_id)
        theme_ids = _require_references(category.get("theme_ids"), themes, "theme_ids", category_id, errors)
        for theme_id in theme_ids:
            theme_memberships.setdefault(theme_id, []).append(category_id)

    all_legacy_ids = set(category_legacy_ids)
    profile_refs: set[str] = set()
    for theme_id, theme in themes.items():
        if not theme_id.startswith("theme."):
            errors.append(f"canonical theme id must use theme.*: {theme_id}")
        category_id = theme.get("category_id")
        theme_modes = theme.get("audience_modes")
        valid_theme_modes = isinstance(theme_modes, list) and all(
            isinstance(mode, str) and mode in CHANNEL_IDS for mode in theme_modes
        )
        if not valid_theme_modes or len(theme_modes) != len(set(theme_modes) if valid_theme_modes else []):
            errors.append(f"theme {theme_id} has invalid audience_modes")
        theme_status = theme.get("status")
        if not _string_is_one_of(theme_status, THEME_STATUSES):
            errors.append(f"theme {theme_id} has invalid status {theme_status}")
        if not _non_empty_string(category_id) or category_id not in categories:
            errors.append(f"theme {theme_id} references unknown category {category_id}")
        else:
            parent_theme_ids = categories[category_id].get("theme_ids", [])
            if not isinstance(parent_theme_ids, list) or theme_id not in parent_theme_ids:
                errors.append(f"theme {theme_id} is missing from parent category {category_id}.theme_ids")
            category_modes = categories[category_id].get("audience_modes", [])
            if valid_theme_modes and isinstance(category_modes, list) and set(theme_modes) != set(category_modes):
                errors.append(f"theme {theme_id} audience_modes must match its parent category")
        memberships = theme_memberships.get(theme_id, [])
        if category_id in categories and memberships != [category_id]:
            errors.append(f"theme {theme_id} must be listed exactly once under its canonical parent")
        if not isinstance(theme.get("labels"), dict) or not _non_empty_string(theme.get("labels", {}).get("zh-CN")):
            errors.append(f"theme {theme_id} requires labels.zh-CN")
        legacy_ids = theme.get("legacy_ids", [])
        if not isinstance(legacy_ids, list) or any(not _non_empty_string(item) for item in legacy_ids):
            errors.append(f"theme {theme_id}.legacy_ids must be strings")
        else:
            for legacy_id in legacy_ids:
                if legacy_id in all_legacy_ids:
                    errors.append(f"duplicate legacy id: {legacy_id}")
                all_legacy_ids.add(legacy_id)
        profile_ref = theme.get("profile_ref")
        if _non_empty_string(profile_ref):
            if profile_ref in profile_refs:
                errors.append(f"duplicate profile_ref: {profile_ref}")
            profile_refs.add(profile_ref)
        elif _string_is_one_of(theme_status, PROFILE_READY_STATUSES):
            errors.append(f"theme {theme_id} with ready profile status requires profile_ref")

        profile_version = theme.get("profile_version")
        if _string_is_one_of(theme_status, PROFILE_READY_STATUSES) and not _non_empty_string(profile_version):
            errors.append(f"theme {theme_id} with ready profile status requires profile_version")
        _validate_source_metadata(theme.get("profile_source"), f"theme {theme_id}.profile_source", errors)

        profile = theme.get("profile")
        _validate_profile(
            profile,
            f"theme {theme_id}.profile",
            errors,
            required=_string_is_one_of(theme_status, PROFILE_COMPLETE_STATUSES),
        )

        overlays = theme.get("audience_profile_overlays")
        expected_overlay_modes = set(theme_modes) if valid_theme_modes else set()
        if not isinstance(overlays, dict) or set(overlays) != expected_overlay_modes:
            errors.append(f"theme {theme_id} must declare one audience overlay for each audience_mode")
            continue
        for mode, overlay in overlays.items():
            if not isinstance(overlay, dict) or overlay.get("status") not in OVERLAY_STATUSES:
                errors.append(f"theme {theme_id} has invalid audience overlay for {mode}")
                continue
            overlay_status = overlay.get("status")
            if not _string_is_one_of(overlay_status, OVERLAY_STATUSES):
                errors.append(f"theme {theme_id} has invalid audience overlay status for {mode}")
                continue
            overlay_profile = overlay.get("profile")
            if overlay_status == "defined":
                if not isinstance(overlay_profile, dict) or set(overlay_profile) != set(PROFILE_DIMENSIONS):
                    errors.append(f"theme {theme_id} defined overlay for {mode} requires all five dimensions")
                elif any(not _non_empty_string(overlay_profile.get(key)) for key in PROFILE_DIMENSIONS):
                    errors.append(f"theme {theme_id} defined overlay for {mode} is incomplete")
            elif overlay_status == "no_difference" and overlay_profile is not None:
                errors.append(f"theme {theme_id} no_difference overlay for {mode} must not duplicate a profile")

    for channel_id, channel in channels.items():
        category_ids = _require_references(channel.get("category_ids"), categories, "category_ids", channel_id, errors)
        featured = _require_references(
            channel.get("featured_category_ids"), categories, "featured_category_ids", channel_id, errors
        )
        if any(category_id not in category_ids for category_id in featured):
            errors.append(f"channel {channel_id} featured categories must be in category_ids")
        if len(featured) > 12:
            errors.append(f"channel {channel_id} may feature at most 12 categories")
        for category_id in category_ids:
            category_modes = categories.get(category_id, {}).get("audience_modes", [])
            if not isinstance(category_modes, list) or channel_id not in category_modes:
                errors.append(f"channel {channel_id} references incompatible category {category_id}")

    relationship_axes = bundle.get("relationship_axes")
    if not isinstance(relationship_axes, list):
        errors.append("relationship_axes must be a list")
        relationship_axis_ids: set[str] = set()
    else:
        relationship_axis_ids = set()
        for index, axis in enumerate(relationship_axes):
            if not isinstance(axis, dict) or not _non_empty_string(axis.get("id")) or not _non_empty_string(axis.get("label")):
                errors.append(f"relationship_axes[{index}] requires id and label")
                continue
            if axis["id"] in relationship_axis_ids:
                errors.append(f"duplicate relationship axis id: {axis['id']}")
            relationship_axis_ids.add(axis["id"])
    for theme_id, theme in themes.items():
        axes = theme.get("supported_relationship_axes", [])
        valid_axes = isinstance(axes, list) and all(isinstance(axis, str) for axis in axes)
        if not valid_axes or any(axis not in relationship_axis_ids for axis in axes):
            errors.append(f"theme {theme_id}.supported_relationship_axes contains unknown ids")
        elif len(axes) != len(set(axes)):
            errors.append(f"theme {theme_id}.supported_relationship_axes contains duplicate ids")

    custom_entry = bundle.get("custom_entry")
    if not isinstance(custom_entry, dict) or custom_entry.get("scope") != "project":
        errors.append("custom_entry must declare project scope")
    else:
        for kind in ("category", "theme"):
            item = custom_entry.get(kind)
            if not isinstance(item, dict) or item.get("enabled") is not True or not _non_empty_string(item.get("id_prefix")):
                errors.append(f"custom_entry.{kind} must enable project-scoped custom IDs")
        if custom_entry.get("profile_dimensions") != list(PROFILE_DIMENSIONS):
            errors.append("custom_entry.profile_dimensions must follow the five-dimension contract")
        if custom_entry.get("draft_status") != "custom_profile_draft":
            errors.append("custom_entry.draft_status must be custom_profile_draft")
        if custom_entry.get("generation_requires_complete_profile") is not True:
            errors.append("custom profile generation must require all dimensions")
        if custom_entry.get("registry_mutation_allowed") is not False:
            errors.append("custom entries must not mutate the built-in registry")

    platforms = _records_by_id(bundle.get("platforms"), "platform", errors)
    for platform_id, platform in platforms.items():
        if not _non_empty_string(platform.get("label")) or not _non_empty_string(platform.get("source_url")):
            errors.append(f"platform {platform_id} requires label and source_url")
        if not str(platform.get("source_url", "")).startswith("https://"):
            errors.append(f"platform {platform_id}.source_url must use https")
        if not _string_is_one_of(platform.get("status"), PLATFORM_STATUSES):
            errors.append(f"platform {platform_id} has invalid status {platform.get('status')}")
        if not _non_empty_string(platform.get("source_file")):
            errors.append(f"platform {platform_id}.source_file must be non-empty")
        platform_observed_at = platform.get("observed_at")
        if platform.get("status") == "observed":
            if not _non_empty_string(platform_observed_at):
                errors.append(f"platform {platform_id} observed status requires observed_at")
            else:
                try:
                    date.fromisoformat(platform_observed_at)
                except (TypeError, ValueError):
                    errors.append(f"platform {platform_id}.observed_at must be an ISO date")
        elif platform_observed_at is not None:
            errors.append(f"platform {platform_id} pending_source_review must not claim observed_at")
        mappings = platform.get("mappings")
        if not isinstance(mappings, list):
            errors.append(f"platform {platform_id}.mappings must be a list")
            continue
        if platform.get("status") == "pending_source_review" and mappings:
            errors.append(f"platform {platform_id} pending_source_review must not have mappings")
        for index, mapping in enumerate(mappings):
            prefix = f"platform {platform_id}.mappings[{index}]"
            if not isinstance(mapping, dict):
                errors.append(f"{prefix} must be an object")
                continue
            for required in ("source_category_label", "source_url", "confidence", "status"):
                if not _non_empty_string(mapping.get(required)):
                    errors.append(f"{prefix}.{required} must be non-empty")
            if not str(mapping.get("source_url", "")).startswith("https://"):
                errors.append(f"{prefix}.source_url must use https")
            mapping_status = mapping.get("status")
            if not _string_is_one_of(mapping_status, MAPPING_STATUSES):
                errors.append(f"{prefix}.status is invalid")
            if not _string_is_one_of(mapping.get("confidence"), {"official_observed", "legacy_mapping"}):
                errors.append(f"{prefix}.confidence is invalid")
            if not _non_empty_string(mapping.get("source_path")):
                errors.append(f"{prefix}.source_path must be non-empty")
            if mapping.get("observed_at") is not None and not _non_empty_string(mapping.get("observed_at")):
                errors.append(f"{prefix}.observed_at must be an ISO date or null")
            elif mapping.get("observed_at") is not None:
                try:
                    date.fromisoformat(mapping["observed_at"])
                except (TypeError, ValueError):
                    errors.append(f"{prefix}.observed_at must be an ISO date or null")
            canonical_category_id = mapping.get("canonical_category_id")
            canonical_theme_id = mapping.get("canonical_theme_id")
            candidates = mapping.get("candidate_category_ids", [])
            if canonical_category_id is not None and (
                not _non_empty_string(canonical_category_id) or canonical_category_id not in categories
            ):
                errors.append(f"{prefix} references unknown canonical category {canonical_category_id}")
            if canonical_theme_id is not None:
                theme = themes.get(canonical_theme_id) if _non_empty_string(canonical_theme_id) else None
                if theme is None:
                    errors.append(f"{prefix} references unknown canonical theme {canonical_theme_id}")
                elif canonical_category_id != theme.get("category_id"):
                    errors.append(f"{prefix} category/theme parents do not match")
            if not isinstance(candidates, list) or any(
                not _non_empty_string(candidate) or candidate not in categories for candidate in candidates
            ):
                errors.append(f"{prefix}.candidate_category_ids contains unknown ids")
            elif len(candidates) != len(set(candidates)):
                errors.append(f"{prefix}.candidate_category_ids contains duplicate ids")
            if canonical_category_id is not None and candidates:
                errors.append(f"{prefix} cannot be both resolved and ambiguous")
            if mapping.get("audience") is not None and not _string_is_one_of(mapping.get("audience"), CHANNEL_IDS):
                errors.append(f"{prefix}.audience is invalid")
            status = mapping.get("status")
            if _string_is_one_of(status, RESOLVED_MAPPING_STATUSES) and canonical_category_id is None:
                errors.append(f"{prefix} resolved status requires canonical_category_id")
            if status == "mapped_theme" and canonical_theme_id is None:
                errors.append(f"{prefix} mapped_theme status requires canonical_theme_id")
            if status == "parent_only_pending_theme" and (canonical_category_id is None or canonical_theme_id is not None):
                errors.append(f"{prefix} parent_only_pending_theme requires category only")
            if status == "ambiguous" and (canonical_category_id is not None or len(candidates) < 2):
                errors.append(f"{prefix} ambiguous status requires multiple candidate categories only")
            if status == "pending_mapping" and (canonical_category_id is not None or canonical_theme_id is not None or candidates):
                errors.append(f"{prefix} pending_mapping must not resolve canonical ids")

    provenance = bundle.get("provenance")
    if not isinstance(provenance, dict):
        errors.append("provenance must be an object")
    else:
        for key in ("taxonomy_source", "market_source", "platform_observation_source", "generated_at", "policy"):
            if not _non_empty_string(provenance.get(key)):
                errors.append(f"provenance.{key} must be non-empty")
        try:
            date.fromisoformat(provenance.get("generated_at", ""))
        except (TypeError, ValueError):
            errors.append("provenance.generated_at must be an ISO date")
        draft_sources = provenance.get("profile_draft_sources")
        if not isinstance(draft_sources, list) or any(not _non_empty_string(item) for item in draft_sources):
            errors.append("provenance.profile_draft_sources must be a list of non-empty paths")

    if errors:
        raise TaxonomyV2ValidationError("; ".join(errors))


def missing_profile_dimensions(profile: Any) -> list[str]:
    """Return five-dimension fields that are absent or blank from a custom profile."""
    if not isinstance(profile, dict):
        return list(PROFILE_DIMENSIONS)
    return [dimension for dimension in PROFILE_DIMENSIONS if not _non_empty_string(profile.get(dimension))]


def resolve_theme_profile_for_generation(
    bundle: Mapping[str, Any], theme_id: str, audience_mode: str
) -> Dict[str, str]:
    """Return a reviewed profile for a theme and audience, or reject it.

    Completeness alone does not make a draft or a migrated legacy profile safe
    for generation. Both the theme and its selected audience overlay must have
    passed review (`ready`, `defined`, or `no_difference`).
    """
    validate_cn_market_v2_bundle(bundle)
    themes = {item["id"]: item for item in bundle["themes"]}
    theme = themes.get(theme_id)
    if theme is None:
        raise TaxonomyV2ValidationError(f"unknown theme id: {theme_id}")
    if theme.get("status") != "ready":
        raise TaxonomyV2ValidationError(f"theme {theme_id} profile is not ready")
    if audience_mode not in theme.get("audience_modes", []):
        raise TaxonomyV2ValidationError(f"theme {theme_id} does not support audience {audience_mode}")
    overlay = theme.get("audience_profile_overlays", {}).get(audience_mode)
    if not isinstance(overlay, dict) or overlay.get("status") not in {"defined", "no_difference"}:
        raise TaxonomyV2ValidationError(f"theme {theme_id} audience profile for {audience_mode} is not ready")
    profile = dict(theme["profile"])
    if overlay["status"] == "defined":
        profile.update(overlay["profile"])
    return profile


def resolve_category_profile_for_generation(bundle: Mapping[str, Any], category_id: str) -> Dict[str, str]:
    """Return a reviewed category profile, blocking legacy and draft defaults."""
    validate_cn_market_v2_bundle(bundle)
    categories = {item["id"]: item for item in bundle["categories"]}
    category = categories.get(category_id)
    if category is None:
        raise TaxonomyV2ValidationError(f"unknown category id: {category_id}")
    if category.get("default_profile_status") != "ready":
        raise TaxonomyV2ValidationError(f"category {category_id} default profile is not ready")
    return dict(category["default_profile"])


def validate_custom_market_profile(profile: Any, *, for_generation: bool = False) -> list[str]:
    """Validate project-scoped custom category/theme data.

    Drafts may be saved with missing dimensions. When ``for_generation`` is true,
    every selected custom node must have all five dimensions. The return value is
    the fully qualified list of missing dimension paths, useful for UI errors.
    """
    if not isinstance(profile, dict):
        raise TaxonomyV2ValidationError("custom_market_profile must be an object")
    if profile.get("scope") != "project":
        raise TaxonomyV2ValidationError("custom_market_profile.scope must be project")
    if profile.get("audience_mode") not in CHANNEL_IDS:
        raise TaxonomyV2ValidationError("custom_market_profile.audience_mode must be male, female, or dual")

    category = profile.get("custom_category")
    theme = profile.get("custom_theme")
    missing: list[str] = []
    if category is not None:
        if not isinstance(category, dict):
            raise TaxonomyV2ValidationError("custom_category must be an object")
        if not _non_empty_string(category.get("id")) or not category["id"].startswith("custom.category."):
            raise TaxonomyV2ValidationError("custom_category.id must use custom.category.*")
        if not _non_empty_string(category.get("label")):
            raise TaxonomyV2ValidationError("custom_category.label must be non-empty")
        parent = category.get("parent_canonical_category_id")
        if parent is not None and not _non_empty_string(parent):
            raise TaxonomyV2ValidationError("custom_category.parent_canonical_category_id must be an id or null")
        if theme is None and for_generation:
            missing.extend(f"custom_category.profile.{key}" for key in missing_profile_dimensions(category.get("profile")))

    if theme is not None:
        if not isinstance(theme, dict):
            raise TaxonomyV2ValidationError("custom_theme must be an object")
        if not _non_empty_string(theme.get("id")) or not theme["id"].startswith("custom.theme."):
            raise TaxonomyV2ValidationError("custom_theme.id must use custom.theme.*")
        if not _non_empty_string(theme.get("label")):
            raise TaxonomyV2ValidationError("custom_theme.label must be non-empty")
        canonical_parent = theme.get("parent_canonical_category_id")
        custom_parent = theme.get("parent_custom_category_id")
        if bool(canonical_parent) == bool(custom_parent):
            raise TaxonomyV2ValidationError("custom_theme must reference exactly one canonical or custom category parent")
        if custom_parent:
            if category is None or custom_parent != category.get("id"):
                raise TaxonomyV2ValidationError("custom_theme parent_custom_category_id must match custom_category.id")
        elif not _non_empty_string(canonical_parent):
            raise TaxonomyV2ValidationError("custom_theme.parent_canonical_category_id must be an id")
        if for_generation:
            missing.extend(f"custom_theme.profile.{key}" for key in missing_profile_dimensions(theme.get("profile")))

    if category is None and theme is None:
        raise TaxonomyV2ValidationError("custom_market_profile must contain a custom category or theme")
    if for_generation and missing:
        raise TaxonomyV2ValidationError("custom profile is incomplete: " + ", ".join(missing))
    return missing


@lru_cache(maxsize=1)
def _load_bundle_cached() -> Dict[str, Any]:
    path = taxonomy_v2_yaml_path()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    bundle = yaml.safe_load(path.read_text(encoding="utf-8"))
    validate_cn_market_v2_bundle(bundle)
    return bundle


def load_cn_market_v2_bundle() -> Dict[str, Any]:
    """Return a validated copy so callers cannot mutate the process-wide cache."""
    return deepcopy(_load_bundle_cached())


def resolve_legacy_taxonomy_id(identifier: str) -> str | None:
    """Resolve a v1 category or theme ID to its v2 canonical ID."""
    bundle = _load_bundle_cached()
    for collection in (bundle["categories"], bundle["themes"]):
        for item in collection:
            if identifier == item["id"] or identifier in item.get("legacy_ids", []):
                return item["id"]
    return None
