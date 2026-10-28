"""Bible onboarding continuation handlers."""
from __future__ import annotations

from typing import Any, Mapping

from application.ai_invocation.continuation import ContinuationContext, register_continuation_handler
from application.ai.llm_json_extract import parse_llm_json_to_any
from application.ai_invocation.output_binding_resolution import (
    extract_bound_output_values,
    load_session_output_bindings,
)
from application.world.dtos.bible_dto import CharacterDTO, LocationDTO, StyleNoteDTO
from application.world.services.generated_location_normalization import prepare_generated_locations
from application.world.services.generated_character_normalization import (
    prepare_generated_characters, _parse_jsonish_value, _as_list, _as_dict, _as_str, _compose_character_description,
)
from application.world.worldbuilding_schema import validate_complete_dimension_fields
from application.onboarding.setup_edit_guard import assert_setup_editable


WORLDBUILDING_DIMENSION_KEYS = ("core_rules", "geography", "society", "culture", "daily_life")


def _context_value(context: Mapping[str, Any], key: str, default: Any = None) -> Any:
    value = context.get(key, default)
    return default if value is None else value


def _parse_content(raw: str) -> dict[str, Any]:
    data, _ = parse_llm_json_to_any(raw)
    return data if isinstance(data, dict) else {}


def _coerce_worldbuilding_dimension(dim_key: str, value: Any) -> dict[str, Any]:
    value = _parse_jsonish_value(value)
    if isinstance(value, Mapping):
        return validate_complete_dimension_fields(dim_key, value)
    return {}


def _extract_records(data: Any, key: str) -> list[Any]:
    if isinstance(data, Mapping):
        records = data.get(key)
    else:
        records = data
    return _as_list(records)


def _bound_value_map(context: ContinuationContext, payload: Any) -> dict[str, Any]:
    bindings = load_session_output_bindings(context.session)
    _, by_variable_key = extract_bound_output_values(payload, bindings)
    return by_variable_key


def _get_services(_context: ContinuationContext):
    from application.paths import get_db_path
    from application.world.services.bible_location_triple_sync import BibleLocationTripleSyncService
    from application.world.services.bible_service import BibleService
    from application.world.services.worldbuilding_service import WorldbuildingService
    from infrastructure.persistence.database.connection import get_database
    from infrastructure.persistence.database.sqlite_bible_repository import SqliteBibleRepository
    from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
    from infrastructure.persistence.database.sqlite_novel_repository import SqliteNovelRepository
    from infrastructure.persistence.database.triple_repository import TripleRepository
    from infrastructure.persistence.database.unified_character_repository import SqliteUnifiedCharacterRepository
    from infrastructure.persistence.database.worldbuilding_repository import WorldbuildingRepository

    db = get_database()
    bible_service = BibleService(
        SqliteBibleRepository(db),
        novel_repository=SqliteNovelRepository(db),
        chapter_repository=SqliteChapterRepository(db),
        location_triple_sync=BibleLocationTripleSyncService(TripleRepository(db)),
        unified_character_repository=SqliteUnifiedCharacterRepository(db),
    )
    worldbuilding_service = WorldbuildingService(WorldbuildingRepository(get_db_path()))
    return bible_service, worldbuilding_service


def _refresh_shared_state(novel_id: str) -> None:
    if not novel_id:
        return
    try:
        from application.engine.services.state_bootstrap import refresh_narrative_contract_in_shared_state

        refresh_narrative_contract_in_shared_state(novel_id)
    except Exception:
        pass


def bible_worldbuilding_handler(context: ContinuationContext) -> Mapping[str, Any]:
    novel_id = str(_context_value(context.session.context, "novel_id", ""))
    if not novel_id:
        return {}
    assert_setup_editable(novel_id, "worldbuilding")
    data = _parse_content(context.decision.accepted_content)
    by_variable_key = _bound_value_map(context, data)
    style = str(by_variable_key.get("worldbuilding.style") or data.get("style") or "").strip()
    bound_worldbuilding = by_variable_key.get("worldbuilding.content")
    worldbuilding_value = bound_worldbuilding if bound_worldbuilding is not None else data.get("worldbuilding")
    worldbuilding_value = _parse_jsonish_value(worldbuilding_value)
    worldbuilding = dict(worldbuilding_value) if isinstance(worldbuilding_value, Mapping) else {}
    for dim_key in WORLDBUILDING_DIMENSION_KEYS:
        dim_value = by_variable_key.get(f"worldbuilding.{dim_key}")
        if dim_value is not None:
            worldbuilding[dim_key] = dim_value
    if not worldbuilding:
        worldbuilding = {
            dim_key: by_variable_key.get(f"worldbuilding.{dim_key}", data.get(dim_key))
            for dim_key in WORLDBUILDING_DIMENSION_KEYS
            if by_variable_key.get(f"worldbuilding.{dim_key}", data.get(dim_key)) is not None
        }
    normalized = {
        dim_key: coerced
        for dim_key, block in worldbuilding.items()
        if dim_key in WORLDBUILDING_DIMENSION_KEYS
        for coerced in [_coerce_worldbuilding_dimension(dim_key, block)]
        if coerced
    }
    if not style and not normalized:
        raise ValueError("世界观生成结果中没有有效世界观或文风，请重试")

    result: dict[str, Any] = {"novel_id": novel_id}
    if style:
        result["style"] = style
    if normalized:
        result["worldbuilding"] = normalized
        for dim_key, dim_value in normalized.items():
            result[dim_key] = dim_value
    bible_service, worldbuilding_service = _get_services(context)
    if normalized:
        worldbuilding_service.update_worldbuilding(
            novel_id=novel_id,
            core_rules=normalized.get("core_rules"),
            geography=normalized.get("geography"),
            society=normalized.get("society"),
            culture=normalized.get("culture"),
            daily_life=normalized.get("daily_life"),
        )
    bible = bible_service.ensure_bible_for_novel(novel_id)
    style_notes = bible.style_notes
    if style:
        style_notes = [
            StyleNoteDTO(
                id=f"{novel_id}-style-1",
                category="文风公约",
                content=style,
            )
        ]
    bible_service.update_bible(
        novel_id=novel_id,
        characters=bible.characters,
        world_settings=bible.world_settings,
        locations=bible.locations,
        timeline_notes=bible.timeline_notes,
        style_notes=style_notes,
    )
    _refresh_shared_state(novel_id)
    return result


def bible_characters_handler(context: ContinuationContext) -> Mapping[str, Any]:
    novel_id = str(_context_value(context.session.context, "novel_id", ""))
    if not novel_id:
        return {}
    assert_setup_editable(novel_id, "characters")
    data, _ = parse_llm_json_to_any(context.decision.accepted_content)
    by_variable_key = _bound_value_map(context, data)
    characters = _as_list(by_variable_key.get("characters.list")) or _extract_records(data, "characters")
    saved, characters = prepare_generated_characters(novel_id, characters)

    if not saved:
        raise ValueError("人物生成结果中没有有效人物，请重试")

    protagonist = saved[0]
    bible_service, _ = _get_services(context)
    bible = bible_service.ensure_bible_for_novel(novel_id)
    bible_service.update_bible(
        novel_id=novel_id,
        characters=characters,
        world_settings=bible.world_settings,
        locations=bible.locations,
        timeline_notes=bible.timeline_notes,
        style_notes=bible.style_notes,
        replace_characters=True,
    )
    _refresh_shared_state(novel_id)
    return {"novel_id": novel_id, "characters": saved, "protagonist": protagonist, "existing_characters": saved}


def bible_locations_handler(context: ContinuationContext) -> Mapping[str, Any]:
    novel_id = str(_context_value(context.session.context, "novel_id", ""))
    if not novel_id:
        return {}
    assert_setup_editable(novel_id, "locations")
    data, _ = parse_llm_json_to_any(context.decision.accepted_content)
    by_variable_key = _bound_value_map(context, data)
    locations = _as_list(by_variable_key.get("locations.list")) or _extract_records(data, "locations")
    saved = [
        {
            "id": row["location_id"],
            "name": row["name"],
            "description": row["description"],
            "type": row["location_type"],
            "parent_id": row["parent_id"],
            "connections": _as_list(row["connections"]),
        }
        for row in prepare_generated_locations(novel_id, locations)
    ]
    if not saved:
        raise ValueError("地点生成结果中没有有效地点，请重试")

    bible_service, _ = _get_services(context)
    bible = bible_service.ensure_bible_for_novel(novel_id)
    locations = [
        LocationDTO(
            id=str(row.get("id") or f"{novel_id}-loc-{idx + 1}"),
            name=str(row.get("name") or "").strip(),
            description=str(row.get("description") or ""),
            location_type=str(row.get("type") or row.get("location_type") or "场景"),
            parent_id=row.get("parent_id"),
        )
        for idx, row in enumerate(saved)
        if str(row.get("name") or "").strip()
    ]
    bible_service.update_bible(
        novel_id=novel_id,
        characters=bible.characters,
        world_settings=bible.world_settings,
        locations=locations,
        timeline_notes=bible.timeline_notes,
        style_notes=bible.style_notes,
    )
    _refresh_shared_state(novel_id)
    return {"novel_id": novel_id, "locations": saved, "existing_locations": saved}


def register_bible_setup_continuations() -> None:
    register_continuation_handler("bible_worldbuilding", bible_worldbuilding_handler)
    register_continuation_handler("bible_characters", bible_characters_handler)
    register_continuation_handler("bible_locations", bible_locations_handler)
