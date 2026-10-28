"""One representation for auditing, saving and verifying generated characters."""
from __future__ import annotations

import json
from typing import Any, Mapping

from application.ai.llm_json_extract import parse_llm_json_to_any
from application.world.dtos.bible_dto import CharacterDTO


def _parse_jsonish_value(value: Any) -> Any:
    if not isinstance(value, str) or not value.strip() or value.strip()[0] not in '[{':
        return value
    parsed, _ = parse_llm_json_to_any(value.strip())
    return parsed if parsed is not None else value


def _as_list(value: Any) -> list[Any]:
    value = _parse_jsonish_value(value)
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value.strip()] if isinstance(value, str) and value.strip() else []


def _as_dict(value: Any) -> dict[str, Any]:
    value = _parse_jsonish_value(value)
    return dict(value) if isinstance(value, Mapping) else {}


def _as_str(value: Any, default: str = '') -> str:
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return default if value is None else str(value)


def _compose_character_description(role: str, description: str) -> str:
    role_text, desc_text = _as_str(role or '').strip(), _as_str(description or '').strip()
    if not role_text:
        return desc_text
    if not desc_text:
        return role_text
    return desc_text if desc_text.startswith(f'{role_text} - ') else f'{role_text} - {desc_text}'


def normalize_character_voice_profile(value, *, preserve_extra=False) -> dict:
    profile = _as_dict(value)
    normalized = {key: _as_str(item) for key, item in {
        'style': profile.get('style') or profile.get('voice_style') or '',
        'sentence_pattern': profile.get('sentence_pattern') or '',
        'speech_tempo': profile.get('speech_tempo') or '',
    }.items() if item}
    if not preserve_extra:
        return normalized
    return {**{key: item for key, item in profile.items()
               if key not in ('style', 'voice_style', 'sentence_pattern', 'speech_tempo')}, **normalized}


def normalize_character_relationships(value) -> list[dict]:
    rows = []
    for item in _as_list(value):
        if isinstance(item, str):
            item = {'target': item}
        if not isinstance(item, Mapping):
            raise ValueError('人物关系必须是名称或包含 target 的对象')
        rows.append({key: _as_str(item.get(key) or '') for key in ('target', 'relation', 'description')})
    return rows


def normalize_character_wounds(value) -> list[dict]:
    rows = []
    for item in _as_list(value):
        if isinstance(item, str):
            item = {'trigger': item, 'effect': ''}
        if not isinstance(item, Mapping):
            raise ValueError('人物创伤必须是文本或结构化对象')
        rows.append(dict(item))
    return rows


def prepare_generated_characters(novel_id: str, records) -> tuple[list[dict], list[CharacterDTO]]:
    saved, used_ids = [], set()
    for index, raw in enumerate(records or []):
        if not isinstance(raw, Mapping) or not str(raw.get('name') or '').strip():
            continue
        row = dict(raw)
        identifier = str(row.get('id') or f'{novel_id}-char-{index + 1}')
        if identifier in used_ids:
            suffix = len(used_ids)
            identifier = f'{novel_id}-char-{index + 1}-{suffix}'
            while identifier in used_ids:
                suffix += 1
                identifier = f'{novel_id}-char-{index + 1}-{suffix}'
        used_ids.add(identifier)
        row.update(id=identifier, name=str(row['name']).strip(),
                   relationships=normalize_character_relationships(row.get('relationships')),
                   moral_taboos=_as_list(row.get('moral_taboos')), voice_profile=_as_dict(row.get('voice_profile')),
                   active_wounds=normalize_character_wounds(row.get('active_wounds')))
        saved.append(row)
    characters = [CharacterDTO(
        id=row['id'], name=row['name'],
        description=_compose_character_description(row.get('role'), row.get('description')),
        relationships=row['relationships'], gender=_as_str(row.get('gender')), age=_as_str(row.get('age')),
        appearance=_as_str(row.get('appearance')), personality=_as_str(row.get('personality') or row.get('flaw')),
        background=_as_str(row.get('background') or row.get('ghost')),
        core_motivation=_as_str(row.get('core_motivation') or row.get('want')),
        inner_lack=_as_str(row.get('inner_lack') or row.get('need')),
        public_profile=_as_str(row.get('public_profile')), hidden_profile=_as_str(row.get('hidden_profile')),
        reveal_chapter=row.get('reveal_chapter'), mental_state=_as_str(row.get('mental_state'), 'NORMAL') or 'NORMAL',
        mental_state_reason=_as_str(row.get('mental_state_reason')), verbal_tic=_as_str(row.get('verbal_tic')),
        idle_behavior=_as_str(row.get('idle_behavior')), core_belief=_as_str(row.get('core_belief')),
        moral_taboos=row['moral_taboos'], voice_profile=normalize_character_voice_profile(row['voice_profile']),
        active_wounds=row['active_wounds'],
    ) for row in saved]
    return saved, characters
